from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def load_gha_deps() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "gha_deps.py"
    spec = importlib.util.spec_from_file_location("gha_deps", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gha_deps = load_gha_deps()


def test_format_uv_requirements_sorts_hashes() -> None:
    text = gha_deps.format_uv_requirements(
        "1.2.3",
        {
            "uv-1.2.3-b.whl": "b" * 64,
            "uv-1.2.3-a.whl": "a" * 64,
        },
    )

    lines = text.splitlines()
    assert lines[0] == "# Generated from .github/gha-deps.json by .github/scripts/gha_deps.py."
    assert lines[1] == "uv==1.2.3 \\"
    assert lines[2] == f"    --hash=sha256:{'a' * 64} \\"
    assert lines[3] == f"    --hash=sha256:{'b' * 64}"


def test_parse_ipsw_checksums_returns_workflow_archives() -> None:
    linux_hash = "1" * 64
    macos_hash = "2" * 64
    checksums = f"""
ignored malformed line
{linux_hash}  ipsw_3.1.685_linux_x86_64.tar.gz
{"3" * 64}  ipsw_3.1.685_linux_x86_64.tar.gz.sbom.json
{macos_hash}  ipsw_3.1.685_macOS_universal.tar.gz
"""

    assert gha_deps.parse_ipsw_checksums(checksums, "3.1.685") == {
        "linux_x86_64": linux_hash,
        "macOS_universal": macos_hash,
    }


def test_parse_ipsw_checksums_requires_all_workflow_archives() -> None:
    with pytest.raises(gha_deps.GhaDependencyError, match="missing ipsw checksums"):
        gha_deps.parse_ipsw_checksums(f"{'1' * 64}  ipsw_3.1.685_linux_x86_64.tar.gz", "3.1.685")


def test_parse_ls_remote_output_prefers_peeled_annotated_tag() -> None:
    direct = "a" * 40
    peeled = "b" * 40
    output = f"{direct}\trefs/tags/v1.0.0\n{peeled}\trefs/tags/v1.0.0^{{}}\n"

    assert gha_deps.parse_ls_remote_output(output, "v1.0.0") == peeled


def test_parse_ls_remote_output_accepts_lightweight_tag() -> None:
    direct = "a" * 40

    assert gha_deps.parse_ls_remote_output(f"{direct}\trefs/tags/v1.0.0\n", "v1.0.0") == direct


def test_update_setup_gcloud_text_updates_only_setup_gcloud_version() -> None:
    text = """jobs:
  test:
    steps:
      - name: Set up Cloud SDK
        uses: google-github-actions/setup-gcloud@abc # v1
        with:
          version: \">= 363.0.0\"
      - name: Other
        with:
          version: \"do-not-touch\"
"""

    new_text, changed = gha_deps.update_setup_gcloud_text(text, "575.0.0")

    assert changed == 1
    assert '          version: "575.0.0"' in new_text
    assert '          version: "do-not-touch"' in new_text


def test_normalize_ipsw_platform_aliases() -> None:
    assert gha_deps.normalize_ipsw_platform("macos") == "macOS_universal"
    assert gha_deps.normalize_ipsw_platform("linux") == "linux_x86_64"
    with pytest.raises(gha_deps.GhaDependencyError, match="Unknown ipsw platform"):
        gha_deps.normalize_ipsw_platform("windows")


def test_download_verified_rejects_non_https_before_download(tmp_path: Path) -> None:
    with pytest.raises(gha_deps.GhaDependencyError, match="https://"):
        gha_deps.download_verified("http://example.com/file", "0" * 64, tmp_path / "file")


def test_install_ipsw_requires_manifest_checksum_for_platform(tmp_path: Path) -> None:
    manifest = {
        "ipsw": {
            "version": "3.1.685",
            "archives": {"macOS_universal": "1" * 64},
        }
    }

    with pytest.raises(gha_deps.GhaDependencyError, match="missing SHA-256"):
        gha_deps.install_ipsw(
            manifest,
            platform="linux",
            install_dir=tmp_path,
            use_sudo=False,
        )


@pytest.fixture
def pip_pin() -> dict[str, str]:
    return {
        "version": "26.2.2",
        "url": "https://example.com/pip-26.2.2-py3-none-any.whl",
        "sha256": "a" * 64,
    }


def test_pinned_pip_environment_verifies_wheel_and_precedes_existing_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pip_pin: dict[str, str]
) -> None:
    downloads = []
    monkeypatch.setenv("PYTHONPATH", "/existing/modules")
    monkeypatch.setattr(gha_deps, "download_verified", lambda *args: downloads.append(args))

    env = gha_deps.pinned_pip_environment({"pip": pip_pin}, tmp_path)

    wheel_path = tmp_path / "pip-26.2.2-py3-none-any.whl"
    assert downloads == [(pip_pin["url"], pip_pin["sha256"], wheel_path)]
    assert env["PYTHONPATH"] == f"{wheel_path}{os.pathsep}/existing/modules"
    assert os.environ["PYTHONPATH"] == "/existing/modules"


def test_pinned_pip_environment_without_existing_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pip_pin: dict[str, str]
) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(gha_deps, "download_verified", lambda *args: None)

    env = gha_deps.pinned_pip_environment({"pip": pip_pin}, tmp_path)

    assert env["PYTHONPATH"] == str(tmp_path / "pip-26.2.2-py3-none-any.whl")
    assert "PYTHONPATH" not in os.environ


@pytest.mark.parametrize("invalid_field,value", [("url", "http://example.com/pip.whl"), ("sha256", "invalid")])
def test_pinned_pip_environment_rejects_invalid_pin_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pip_pin: dict[str, str], invalid_field: str, value: str
) -> None:
    downloads = []
    monkeypatch.setattr(gha_deps, "download_verified", lambda *args: downloads.append(args))
    pip_pin[invalid_field] = value

    with pytest.raises(gha_deps.GhaDependencyError):
        gha_deps.pinned_pip_environment({"pip": pip_pin}, tmp_path)
    assert downloads == []


@pytest.mark.parametrize("fail_install", [False, True])
def test_install_uv_uses_pinned_pip_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pip_pin: dict[str, str], fail_install: bool
) -> None:
    manifest = {"pip": pip_pin, "uv": {"version": "1.2.3", "wheel_hashes": {"uv.whl": "b" * 64}}}
    monkeypatch.delenv("GITHUB_PATH", raising=False)
    monkeypatch.setattr(gha_deps, "runner_temp_dir", lambda prefix: tmp_path)
    monkeypatch.setattr(gha_deps, "download_verified", lambda *args: None)
    calls = []

    def run_command(command: list[str], *, stdout_to_stderr: bool, env: dict[str, str]) -> None:
        calls.append(command)
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / "pip-26.2.2-py3-none-any.whl")
        assert env["PIP_BREAK_SYSTEM_PACKAGES"] == "1"
        assert stdout_to_stderr
        assert "--require-hashes" in command
        assert "--use-deprecated=legacy-certs" in command
        assert (tmp_path / "uv-bootstrap.txt").read_text() == gha_deps.format_uv_requirements(
            "1.2.3", {"uv.whl": "b" * 64}
        )
        if fail_install:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(gha_deps, "run_command", run_command)
    if fail_install:
        with pytest.raises(subprocess.CalledProcessError):
            gha_deps.install_uv(manifest, emit_shell=True, print_bin_dir=False)
    else:
        gha_deps.install_uv(manifest, emit_shell=True, print_bin_dir=False)

    assert len(calls) == 1
    assert not tmp_path.exists()


def test_install_uv_does_not_run_pip_if_its_wheel_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pip_pin: dict[str, str]
) -> None:
    manifest = {"pip": pip_pin, "uv": {"version": "1.2.3", "wheel_hashes": {"uv.whl": "b" * 64}}}
    monkeypatch.setattr(gha_deps, "runner_temp_dir", lambda prefix: tmp_path)
    calls = []
    monkeypatch.setattr(gha_deps, "run_command", lambda *args, **kwargs: calls.append(args))

    def reject_download(*args: object) -> None:
        raise gha_deps.GhaDependencyError("SHA-256 mismatch")

    monkeypatch.setattr(gha_deps, "download_verified", reject_download)
    with pytest.raises(gha_deps.GhaDependencyError, match="SHA-256 mismatch"):
        gha_deps.install_uv(manifest, emit_shell=True, print_bin_dir=False)

    assert calls == []
    assert not tmp_path.exists()


def test_verify_network_uses_pinned_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = gha_deps.load_manifest()
    pip_version, _, _ = gha_deps.pip_manifest_values(manifest)
    monkeypatch.setattr(gha_deps, "runner_temp_dir", lambda prefix: tmp_path)
    monkeypatch.setattr(gha_deps, "download_verified", lambda *args: None)
    hashes = iter([manifest["apple_root"]["sha256"], manifest["symsorter"]["sha256"]])
    monkeypatch.setattr(gha_deps, "sha256_url", lambda url: next(hashes))
    calls = []

    def run_command(command: list[str], *, env: dict[str, str]) -> None:
        calls.append(command)
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / f"pip-{pip_version}-py3-none-any.whl")
        assert "download" in command
        assert "--require-hashes" in command
        assert "--use-deprecated=legacy-certs" in command

    monkeypatch.setattr(gha_deps, "run_command", run_command)
    gha_deps.verify(manifest, network=True, large_downloads=False)

    assert len(calls) == 1
    assert not tmp_path.exists()


def test_fetch_pip_wheel_selects_universal_wheel(monkeypatch: pytest.MonkeyPatch, pip_pin: dict[str, str]) -> None:
    urls = []

    def fetch_json(url: str) -> dict[str, object]:
        urls.append(url)
        return {
            "urls": [
                {"filename": "pip-26.2.2.tar.gz"},
                {
                    "filename": "pip-26.2.2-py3-none-any.whl",
                    "url": pip_pin["url"],
                    "digests": {"sha256": pip_pin["sha256"]},
                },
            ]
        }

    monkeypatch.setattr(gha_deps, "fetch_json", fetch_json)

    assert gha_deps.fetch_pip_wheel("26.2.2") == pip_pin
    assert urls == ["https://pypi.org/pypi/pip/26.2.2/json"]


def test_fetch_pip_wheel_requires_universal_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gha_deps, "fetch_json", lambda url: {"urls": [{"filename": "pip-26.2.2.tar.gz"}]})
    with pytest.raises(gha_deps.GhaDependencyError, match="no universal pip wheel"):
        gha_deps.fetch_pip_wheel("26.2.2")


def test_pinned_pip_requires_supported_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(gha_deps.sys, "version_info", (3, 9))
        with pytest.raises(gha_deps.GhaDependencyError, match="Python 3.10"):
            gha_deps.pinned_pip_environment({}, tmp_path)


def test_manifest_requires_pinned_pip() -> None:
    manifest = gha_deps.load_manifest()
    manifest.pop("pip", None)
    with pytest.raises(gha_deps.GhaDependencyError, match="pip"):
        gha_deps.validate_manifest(manifest)


def test_repo_manifest_is_valid() -> None:
    gha_deps.validate_manifest(gha_deps.load_manifest())
