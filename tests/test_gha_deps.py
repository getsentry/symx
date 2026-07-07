from __future__ import annotations

import importlib.util
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


def test_repo_manifest_is_valid() -> None:
    gha_deps.validate_manifest(gha_deps.load_manifest())
