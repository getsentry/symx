#!/usr/bin/env python3
"""Manage pinned GitHub Actions bootstrap dependencies.

This script intentionally uses only the Python standard library. It is called by
GitHub Actions before project dependencies are installed, and it also provides
operator-facing helpers for bumping the pins in .github/gha-deps.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / ".github" / "gha-deps.json"
GITHUB_WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CHUNK_SIZE = 1024 * 1024
DEFAULT_RETRIES = 6
DEFAULT_RETRY_DELAY_SECONDS = 10
DEFAULT_TIMEOUT_SECONDS = 60

IPSW_ARCHIVE_NAMES = {
    "linux_x86_64": "linux_x86_64",
    "macOS_universal": "macOS_universal",
}
IPSW_PLATFORM_ALIASES = {
    "linux": "linux_x86_64",
    "ubuntu": "linux_x86_64",
    "linux_x86_64": "linux_x86_64",
    "macos": "macOS_universal",
    "darwin": "macOS_universal",
    "macOS_universal": "macOS_universal",
}


class GhaDependencyError(RuntimeError):
    """Raised for expected GitHub Actions dependency management failures."""


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def require_sha256(value: str, *, label: str = "SHA-256") -> str:
    normalized = value.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise GhaDependencyError(f"{label} must be exactly 64 hex characters")
    return normalized


def require_https_url(url: str) -> None:
    if not url.startswith("https://"):
        raise GhaDependencyError(f"download URL must use https://: {url}")


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def save_manifest(manifest: dict[str, Any], path: Path = MANIFEST_PATH) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def manifest_section(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    section = manifest.get(name)
    if not isinstance(section, dict):
        raise GhaDependencyError(f"missing or invalid manifest section: {name}")
    return section


def manifest_str(section: dict[str, Any], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise GhaDependencyError(f"missing or invalid manifest value: {key}")
    return value


def manifest_str_map(section: dict[str, Any], key: str) -> dict[str, str]:
    value = section.get(key)
    if not isinstance(value, dict):
        raise GhaDependencyError(f"missing or invalid manifest map: {key}")
    result: dict[str, str] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str) or not isinstance(item_value, str):
            raise GhaDependencyError(f"manifest map {key} must contain string keys and values")
        result[item_key] = item_value
    return result


def request_for(url: str) -> urllib.request.Request:
    require_https_url(url)
    return urllib.request.Request(url, headers={"User-Agent": "symx-gha-deps"})


def retrying_urlopen(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Any:
    last_error: BaseException | None = None
    for attempt in range(1, DEFAULT_RETRIES + 1):
        try:
            return urllib.request.urlopen(request_for(url), timeout=timeout)  # noqa: S310 - URL is required to be HTTPS.
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            last_error = error
            if attempt == DEFAULT_RETRIES:
                break
            eprint(
                f"Download attempt {attempt}/{DEFAULT_RETRIES} failed for {url}: {error}; "
                f"retrying in {DEFAULT_RETRY_DELAY_SECONDS}s"
            )
            time.sleep(DEFAULT_RETRY_DELAY_SECONDS)
    raise GhaDependencyError(f"failed to download {url}: {last_error}")


def fetch_json(url: str) -> dict[str, Any]:
    with retrying_urlopen(url) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise GhaDependencyError(f"expected JSON object from {url}")
    return data


def fetch_text(url: str) -> str:
    with retrying_urlopen(url) as response:
        return response.read().decode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_url(url: str) -> str:
    digest = hashlib.sha256()
    with retrying_urlopen(url) as response:
        while chunk := response.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def download_url(url: str, output_path: Path) -> None:
    eprint(f"Downloading {url}")
    with retrying_urlopen(url) as response, output_path.open("wb") as output:
        shutil.copyfileobj(response, output, length=CHUNK_SIZE)


def runner_temp_dir(prefix: str) -> Path:
    root = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    return Path(tempfile.mkdtemp(prefix=f"{prefix}.", dir=root))


def download_verified(url: str, expected_sha256: str, output_path: Path) -> str:
    expected_sha256 = require_sha256(expected_sha256)
    temp_dir = runner_temp_dir("gha-deps-download")
    try:
        temp_path = temp_dir / "download"
        download_url(url, temp_path)
        actual_sha256 = sha256_file(temp_path)
        if actual_sha256 != expected_sha256:
            raise GhaDependencyError(
                f"SHA-256 mismatch for {url}\n  expected: {expected_sha256}\n  actual:   {actual_sha256}"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(temp_path), output_path)
        eprint(f"Verified SHA-256: {actual_sha256}")
        return actual_sha256
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_command(command: list[str], *, stdout_to_stderr: bool = False, env: dict[str, str] | None = None) -> None:
    stdout = sys.stderr if stdout_to_stderr else None
    subprocess.run(command, check=True, env=env, stdout=stdout, stderr=sys.stderr if stdout_to_stderr else None)


def format_uv_requirements(version: str, wheel_hashes: dict[str, str]) -> str:
    if not wheel_hashes:
        raise GhaDependencyError("uv wheel hash list must not be empty")
    hashes = [
        require_sha256(value, label=f"uv hash for {filename}") for filename, value in sorted(wheel_hashes.items())
    ]
    lines = [
        "# Generated from .github/gha-deps.json by .github/scripts/gha_deps.py.",
        f"uv=={version} \\",
    ]
    for index, digest in enumerate(hashes):
        suffix = " \\" if index < len(hashes) - 1 else ""
        lines.append(f"    --hash=sha256:{digest}{suffix}")
    return "\n".join(lines) + "\n"


def uv_manifest_values(manifest: dict[str, Any]) -> tuple[str, dict[str, str]]:
    uv_section = manifest_section(manifest, "uv")
    return manifest_str(uv_section, "version"), manifest_str_map(uv_section, "wheel_hashes")


def fetch_uv_wheel_hashes(version: str) -> dict[str, str]:
    data = fetch_json(f"https://pypi.org/pypi/uv/{version}/json")
    urls = data.get("urls")
    if not isinstance(urls, list):
        raise GhaDependencyError("PyPI uv response did not contain a urls list")
    hashes: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, dict) or item.get("packagetype") != "bdist_wheel":
            continue
        filename = item.get("filename")
        digests = item.get("digests")
        if not isinstance(filename, str) or not isinstance(digests, dict):
            continue
        sha256 = digests.get("sha256")
        if isinstance(sha256, str):
            hashes[filename] = require_sha256(sha256, label=f"uv hash for {filename}")
    if not hashes:
        raise GhaDependencyError(f"no uv wheel hashes found for {version}")
    return hashes


def install_uv(manifest: dict[str, Any], *, emit_shell: bool, print_bin_dir: bool) -> None:
    version, wheel_hashes = uv_manifest_values(manifest)
    temp_dir = runner_temp_dir("uv-bootstrap")
    try:
        requirements_path = temp_dir / "uv-bootstrap.txt"
        requirements_path.write_text(format_uv_requirements(version, wheel_hashes))
        env = os.environ.copy()
        env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"
        run_command(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--user",
                "--disable-pip-version-check",
                "--no-deps",
                "--only-binary=:all:",
                "--require-hashes",
                "-r",
                str(requirements_path),
            ],
            stdout_to_stderr=emit_shell,
            env=env,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    uv_bin_dir = str(Path(site.getuserbase()) / "bin")
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with Path(github_path).open("a") as file:
            file.write(uv_bin_dir + "\n")
    if emit_shell:
        print(f"export PATH={shlex.quote(uv_bin_dir)}:$PATH")
    if print_bin_dir:
        print(uv_bin_dir)


def symbolicator_asset_url(version: str, asset: str) -> str:
    return f"https://github.com/getsentry/symbolicator/releases/download/{version}/{asset}"


def symsorter_manifest_values(manifest: dict[str, Any]) -> tuple[str, str, str]:
    symsorter = manifest_section(manifest, "symsorter")
    return (
        manifest_str(symsorter, "symbolicator_version"),
        manifest_str(symsorter, "asset"),
        require_sha256(manifest_str(symsorter, "sha256"), label="symsorter SHA-256"),
    )


def install_symsorter(manifest: dict[str, Any], output: Path) -> None:
    version, asset, expected_sha256 = symsorter_manifest_values(manifest)
    download_verified(symbolicator_asset_url(version, asset), expected_sha256, output)
    output.chmod(output.stat().st_mode | 0o111)
    run_command(["file", str(output.resolve())])
    run_command([str(output.resolve()), "--version"])


def normalize_ipsw_platform(platform: str) -> str:
    normalized = IPSW_PLATFORM_ALIASES.get(platform)
    if normalized is None:
        raise GhaDependencyError(f"Unknown ipsw platform: {platform}; expected one of: macos, linux")
    return normalized


def ipsw_archive_url(version: str, archive_platform: str) -> str:
    return f"https://github.com/blacktop/ipsw/releases/download/v{version}/ipsw_{version}_{archive_platform}.tar.gz"


def ipsw_manifest_values(manifest: dict[str, Any]) -> tuple[str, dict[str, str]]:
    ipsw = manifest_section(manifest, "ipsw")
    archives = manifest_str_map(ipsw, "archives")
    for platform, digest in archives.items():
        require_sha256(digest, label=f"ipsw {platform} SHA-256")
    return manifest_str(ipsw, "version"), archives


def install_ipsw(
    manifest: dict[str, Any],
    *,
    platform: str,
    install_dir: Path,
    use_sudo: bool,
) -> None:
    version, manifest_archives = ipsw_manifest_values(manifest)
    version = version.removeprefix("v")
    if not version:
        raise GhaDependencyError("ipsw version must not be empty")
    archive_platform = normalize_ipsw_platform(platform)
    expected_sha256 = manifest_archives.get(archive_platform)
    if expected_sha256 is None:
        raise GhaDependencyError(f"missing SHA-256 for ipsw {version} {archive_platform}")

    temp_dir = runner_temp_dir("ipsw")
    try:
        archive_path = temp_dir / "ipsw.tar.gz"
        url = ipsw_archive_url(version, archive_platform)
        eprint(f"Installing ipsw {version} from {url}")
        download_verified(url, expected_sha256, archive_path)
        command = ["tar", "-xzf", str(archive_path), "-C", str(install_dir), "ipsw"]
        if use_sudo:
            command.insert(0, "sudo")
        run_command(command)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    installed_ipsw = install_dir / "ipsw"
    if installed_ipsw.exists():
        run_command([str(installed_ipsw), "version"])
    else:
        run_command(["ipsw", "version"])


def apple_root_manifest_values(manifest: dict[str, Any]) -> tuple[str, str]:
    apple_root = manifest_section(manifest, "apple_root")
    return (
        manifest_str(apple_root, "url"),
        require_sha256(manifest_str(apple_root, "sha256"), label="Apple root SHA-256"),
    )


def install_apple_root(manifest: dict[str, Any], *, cert_path: Path, crt_path: Path) -> None:
    url, expected_sha256 = apple_root_manifest_values(manifest)
    download_verified(url, expected_sha256, cert_path)
    run_command(["openssl", "x509", "-inform", "DER", "-in", str(cert_path), "-out", str(crt_path)])
    run_command(["sudo", "cp", str(crt_path), "/usr/local/share/ca-certificates"])
    run_command(["sudo", "update-ca-certificates"])


def fetch_ipsw_checksums(version: str) -> dict[str, str]:
    checksum_url = f"https://github.com/blacktop/ipsw/releases/download/v{version}/checksums.txt"
    return parse_ipsw_checksums(fetch_text(checksum_url), version)


def parse_ipsw_checksums(checksums_text: str, version: str) -> dict[str, str]:
    expected_names = {
        platform: f"ipsw_{version}_{archive_name}.tar.gz" for platform, archive_name in IPSW_ARCHIVE_NAMES.items()
    }
    result: dict[str, str] = {}
    for line in checksums_text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, filename = parts
        for platform, expected_name in expected_names.items():
            if filename == expected_name:
                result[platform] = require_sha256(digest, label=f"ipsw {platform} SHA-256")
    missing = sorted(set(expected_names) - set(result))
    if missing:
        raise GhaDependencyError(f"missing ipsw checksums for: {', '.join(missing)}")
    return result


def fetch_gcloud_latest_version() -> str:
    data = fetch_json("https://dl.google.com/dl/cloudsdk/channels/rapid/components-2.json")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise GhaDependencyError("Cloud SDK metadata did not contain a version")
    return version


def update_setup_gcloud_text(text: str, version: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    changed = 0
    waiting_for_version = False
    for index, line in enumerate(lines):
        if "uses: google-github-actions/setup-gcloud@" in line:
            waiting_for_version = True
            continue
        if waiting_for_version:
            match = re.match(r'(?P<indent>\s*)version:\s*["\'][^"\']+["\'](?P<newline>\r?\n?)$', line)
            if match:
                lines[index] = f'{match.group("indent")}version: "{version}"{match.group("newline")}'
                changed += 1
                waiting_for_version = False
            elif re.match(r"\s*-\s+name:", line):
                waiting_for_version = False
    return "".join(lines), changed


def update_gcloud_workflows(version: str, *, write: bool) -> int:
    total_changed = 0
    for path in sorted(GITHUB_WORKFLOWS_DIR.glob("*.yml")):
        text = path.read_text()
        if "google-github-actions/setup-gcloud@" not in text:
            continue
        new_text, changed = update_setup_gcloud_text(text, version)
        if changed:
            total_changed += changed
            print(f"{path.relative_to(REPO_ROOT)}: set Cloud SDK version to {version}")
            if write:
                path.write_text(new_text)
    return total_changed


def parse_ls_remote_output(output: str, tag: str) -> str:
    direct_ref = f"refs/tags/{tag}"
    peeled_ref = f"refs/tags/{tag}^{{}}"
    direct_sha: str | None = None
    peeled_sha: str | None = None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref == direct_ref:
            direct_sha = sha
        elif ref == peeled_ref:
            peeled_sha = sha
    resolved = peeled_sha or direct_sha
    if resolved is None:
        raise GhaDependencyError(f"could not resolve tag {tag}")
    return resolved


def resolve_action_tag(repo: str, tag: str) -> str:
    result = subprocess.run(
        [
            "git",
            "ls-remote",
            f"https://github.com/{repo}.git",
            f"refs/tags/{tag}",
            f"refs/tags/{tag}^{{}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_ls_remote_output(result.stdout, tag)


def validate_manifest(manifest: dict[str, Any]) -> None:
    uv_version, uv_hashes = uv_manifest_values(manifest)
    if not uv_version:
        raise GhaDependencyError("uv version must not be empty")
    for filename, digest in uv_hashes.items():
        require_sha256(digest, label=f"uv hash for {filename}")
    gcloud = manifest_section(manifest, "gcloud")
    manifest_str(gcloud, "version")
    ipsw_manifest_values(manifest)
    symsorter_manifest_values(manifest)
    apple_root_manifest_values(manifest)


def workflow_gcloud_versions() -> dict[Path, list[str]]:
    versions: dict[Path, list[str]] = {}
    version_re = re.compile(r'\s*version:\s*["\']([^"\']+)["\']')
    for path in sorted(GITHUB_WORKFLOWS_DIR.glob("*.yml")):
        text = path.read_text()
        if "google-github-actions/setup-gcloud@" not in text:
            continue
        found: list[str] = []
        waiting_for_version = False
        for line in text.splitlines():
            if "uses: google-github-actions/setup-gcloud@" in line:
                waiting_for_version = True
                continue
            if waiting_for_version:
                match = version_re.match(line)
                if match:
                    found.append(match.group(1))
                    waiting_for_version = False
                elif re.match(r"\s*-\s+name:", line):
                    waiting_for_version = False
        versions[path] = found
    return versions


def scan_static_policy() -> None:
    disallowed_exact = [
        "astral.sh/uv" + "/install.sh",
        "releases/latest" + "/download",
    ]
    curl_word = "cu" + "rl"
    wget_word = "w" + "get"
    disallowed_regex = [
        re.compile(rf"{curl_word}\s+.*\|\s*sh"),
        re.compile(rf"\b{wget_word}\b"),
    ]
    roots = [REPO_ROOT / ".github" / "workflows", REPO_ROOT / ".github" / "actions"]
    failures: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(errors="ignore")
            for needle in disallowed_exact:
                if needle in text:
                    failures.append(f"{path.relative_to(REPO_ROOT)} contains {needle}")
            for pattern in disallowed_regex:
                if pattern.search(text):
                    failures.append(f"{path.relative_to(REPO_ROOT)} matches {pattern.pattern}")
    if failures:
        raise GhaDependencyError("static GitHub Actions dependency policy failures:\n" + "\n".join(failures))


def verify(manifest: dict[str, Any], *, network: bool, large_downloads: bool) -> None:
    validate_manifest(manifest)
    scan_static_policy()
    gcloud_version = manifest_str(manifest_section(manifest, "gcloud"), "version")
    for path, versions in workflow_gcloud_versions().items():
        if not versions:
            raise GhaDependencyError(f"{path.relative_to(REPO_ROOT)} uses setup-gcloud without an explicit version")
        for version in versions:
            if version != gcloud_version:
                raise GhaDependencyError(
                    f"{path.relative_to(REPO_ROOT)} pins Cloud SDK {version}, expected {gcloud_version}"
                )
    print("Static GitHub Actions dependency policy checks passed.", flush=True)

    if not network:
        return

    print("Verifying uv hash coverage for the current platform...", flush=True)
    temp_dir = runner_temp_dir("uv-verify")
    try:
        uv_version, uv_hashes = uv_manifest_values(manifest)
        requirements_path = temp_dir / "uv-bootstrap.txt"
        requirements_path.write_text(format_uv_requirements(uv_version, uv_hashes))
        run_command(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--no-deps",
                "--only-binary=:all:",
                "--require-hashes",
                "-r",
                str(requirements_path),
                "-d",
                str(temp_dir / "uv-download"),
            ]
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("Verifying Apple root certificate...", flush=True)
    apple_url, apple_sha = apple_root_manifest_values(manifest)
    if sha256_url(apple_url) != apple_sha:
        raise GhaDependencyError("Apple root certificate hash mismatch")

    print("Verifying symsorter release asset...", flush=True)
    symsorter_version, symsorter_asset, symsorter_sha = symsorter_manifest_values(manifest)
    if sha256_url(symbolicator_asset_url(symsorter_version, symsorter_asset)) != symsorter_sha:
        raise GhaDependencyError("symsorter hash mismatch")

    if large_downloads:
        print("Verifying ipsw release archives...", flush=True)
        ipsw_version, archives = ipsw_manifest_values(manifest)
        for platform, expected_sha in sorted(archives.items()):
            if sha256_url(ipsw_archive_url(ipsw_version, platform)) != expected_sha:
                raise GhaDependencyError(f"ipsw {platform} hash mismatch")


def print_manifest_update(name: str, old: Any, new: Any, *, write: bool) -> None:
    action = "Updated" if write else "Would update"
    print(f"{action} {name}:")
    print(f"  old: {old}")
    print(f"  new: {new}")


def command_bootstrap_uv(args: argparse.Namespace) -> None:
    install_uv(load_manifest(), emit_shell=args.emit_shell, print_bin_dir=args.print_bin_dir)


def command_download_verified(args: argparse.Namespace) -> None:
    download_verified(args.url, args.sha256, args.output)


def command_install_symsorter(args: argparse.Namespace) -> None:
    install_symsorter(load_manifest(), args.output)


def command_install_ipsw(args: argparse.Namespace) -> None:
    install_ipsw(
        load_manifest(),
        platform=args.platform,
        install_dir=args.install_dir,
        use_sudo=args.use_sudo,
    )


def command_install_apple_root(args: argparse.Namespace) -> None:
    install_apple_root(load_manifest(), cert_path=args.cert_path, crt_path=args.crt_path)


def command_uv_hashes(args: argparse.Namespace) -> None:
    hashes = fetch_uv_wheel_hashes(args.version)
    for filename, digest in sorted(hashes.items()):
        print(filename, digest)


def command_bump_uv(args: argparse.Namespace) -> None:
    manifest = load_manifest()
    old = manifest_section(manifest, "uv")
    new = {"version": args.version, "wheel_hashes": fetch_uv_wheel_hashes(args.version)}
    print_manifest_update("uv", old, new, write=args.write)
    if args.write:
        manifest["uv"] = new
        save_manifest(manifest)


def command_render_uv_requirements(args: argparse.Namespace) -> None:
    version, hashes = uv_manifest_values(load_manifest())
    print(format_uv_requirements(version, hashes), end="")


def command_gcloud_latest(_: argparse.Namespace) -> None:
    print(fetch_gcloud_latest_version())


def command_bump_gcloud(args: argparse.Namespace) -> None:
    version = fetch_gcloud_latest_version() if args.latest else args.version
    if not version:
        raise GhaDependencyError("provide --version or --latest")
    manifest = load_manifest()
    old = manifest_section(manifest, "gcloud")
    new = {"version": version}
    print_manifest_update("Google Cloud SDK", old, new, write=args.write)
    changed = update_gcloud_workflows(version, write=args.write)
    if changed == 0:
        raise GhaDependencyError("no setup-gcloud version pins found to update")
    if args.write:
        manifest["gcloud"] = new
        save_manifest(manifest)


def command_ipsw_checksums(args: argparse.Namespace) -> None:
    for platform, digest in sorted(fetch_ipsw_checksums(args.version).items()):
        print(platform, digest)


def command_bump_ipsw(args: argparse.Namespace) -> None:
    manifest = load_manifest()
    old = manifest_section(manifest, "ipsw")
    new = {"version": args.version.removeprefix("v"), "archives": fetch_ipsw_checksums(args.version.removeprefix("v"))}
    print_manifest_update("ipsw", old, new, write=args.write)
    if args.write:
        manifest["ipsw"] = new
        save_manifest(manifest)


def command_symsorter_sha(args: argparse.Namespace) -> None:
    asset = args.asset
    digest = sha256_url(symbolicator_asset_url(args.version, asset))
    print(args.version, asset, digest)


def command_bump_symsorter(args: argparse.Namespace) -> None:
    asset = args.asset
    digest = sha256_url(symbolicator_asset_url(args.version, asset))
    manifest = load_manifest()
    old = manifest_section(manifest, "symsorter")
    new = {"symbolicator_version": args.version, "asset": asset, "sha256": digest}
    print_manifest_update("symsorter", old, new, write=args.write)
    if args.write:
        manifest["symsorter"] = new
        save_manifest(manifest)


def command_resolve_action(args: argparse.Namespace) -> None:
    print(resolve_action_tag(args.repo, args.tag))


def command_apple_root_sha(args: argparse.Namespace) -> None:
    manifest = load_manifest()
    url, _ = apple_root_manifest_values(manifest)
    if args.details:
        temp_dir = runner_temp_dir("apple-root")
        try:
            cert_path = temp_dir / "AppleIncRootCertificate.cer"
            download_url(url, cert_path)
            print(sha256_file(cert_path))
            run_command(
                [
                    "openssl",
                    "x509",
                    "-inform",
                    "DER",
                    "-in",
                    str(cert_path),
                    "-noout",
                    "-subject",
                    "-issuer",
                    "-fingerprint",
                    "-sha256",
                ]
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        print(sha256_url(url))


def command_bump_apple_root(args: argparse.Namespace) -> None:
    manifest = load_manifest()
    apple_root = manifest_section(manifest, "apple_root")
    url = manifest_str(apple_root, "url")
    digest = sha256_url(url)
    old = dict(apple_root)
    new = {"url": url, "sha256": digest}
    print_manifest_update("Apple root certificate", old, new, write=args.write)
    if args.write:
        manifest["apple_root"] = new
        save_manifest(manifest)


def command_verify(args: argparse.Namespace) -> None:
    verify(load_manifest(), network=args.network, large_downloads=args.large_downloads)


def command_show(_: argparse.Namespace) -> None:
    print(json.dumps(load_manifest(), indent=2, sort_keys=True))


def add_write_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--write", action="store_true", help="write changes instead of only printing the proposed update"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_uv = subparsers.add_parser("bootstrap-uv", help="install the pinned uv bootstrap package")
    bootstrap_uv.add_argument("--emit-shell", action="store_true", help="print shell code exporting uv's bin dir")
    bootstrap_uv.add_argument("--print-bin-dir", action="store_true", help="print uv's bin dir")
    bootstrap_uv.set_defaults(func=command_bootstrap_uv)

    download = subparsers.add_parser("download-verified", help="download a URL and verify its SHA-256")
    download.add_argument("url")
    download.add_argument("sha256")
    download.add_argument("output", type=Path)
    download.set_defaults(func=command_download_verified)

    symsorter = subparsers.add_parser("install-symsorter", help="install the pinned symsorter binary")
    symsorter.add_argument("--output", type=Path, default=Path("symsorter"))
    symsorter.set_defaults(func=command_install_symsorter)

    ipsw = subparsers.add_parser("install-ipsw", help="install the pinned ipsw binary")
    ipsw.add_argument("--platform", required=True)
    ipsw.add_argument("--install-dir", type=Path, default=Path("/usr/local/bin"))
    ipsw.add_argument("--sudo", dest="use_sudo", action="store_true", help="extract with sudo")
    ipsw.set_defaults(func=command_install_ipsw)

    apple_root = subparsers.add_parser("install-apple-root", help="install the pinned Apple root certificate")
    apple_root.add_argument("--cert-path", type=Path, default=Path("AppleIncRootCertificate.cer"))
    apple_root.add_argument("--crt-path", type=Path, default=Path("AppleIncRootCertificate.crt"))
    apple_root.set_defaults(func=command_install_apple_root)

    uv_hashes = subparsers.add_parser("uv-hashes", help="print PyPI wheel hashes for a uv version")
    uv_hashes.add_argument("--version", required=True)
    uv_hashes.set_defaults(func=command_uv_hashes)

    bump_uv = subparsers.add_parser("bump-uv", help="update the uv manifest pin")
    bump_uv.add_argument("--version", required=True)
    add_write_flag(bump_uv)
    bump_uv.set_defaults(func=command_bump_uv)

    render_uv = subparsers.add_parser("render-uv-requirements", help="render the hashed uv requirements file")
    render_uv.set_defaults(func=command_render_uv_requirements)

    gcloud_latest = subparsers.add_parser("gcloud-latest", help="print the latest Google Cloud SDK rapid version")
    gcloud_latest.set_defaults(func=command_gcloud_latest)

    bump_gcloud = subparsers.add_parser("bump-gcloud", help="update the Cloud SDK manifest and workflow pins")
    bump_gcloud.add_argument("--version")
    bump_gcloud.add_argument("--latest", action="store_true")
    add_write_flag(bump_gcloud)
    bump_gcloud.set_defaults(func=command_bump_gcloud)

    ipsw_checksums = subparsers.add_parser(
        "ipsw-checksums", help="print workflow archive checksums for an ipsw version"
    )
    ipsw_checksums.add_argument("--version", required=True)
    ipsw_checksums.set_defaults(func=command_ipsw_checksums)

    bump_ipsw = subparsers.add_parser("bump-ipsw", help="update the ipsw manifest pin")
    bump_ipsw.add_argument("--version", required=True)
    add_write_flag(bump_ipsw)
    bump_ipsw.set_defaults(func=command_bump_ipsw)

    symsorter_sha = subparsers.add_parser("symsorter-sha", help="print the symsorter asset hash for a release")
    symsorter_sha.add_argument("--version", required=True)
    symsorter_sha.add_argument("--asset", default="symsorter-Darwin-universal")
    symsorter_sha.set_defaults(func=command_symsorter_sha)

    bump_symsorter = subparsers.add_parser("bump-symsorter", help="update the symsorter manifest pin")
    bump_symsorter.add_argument("--version", required=True)
    bump_symsorter.add_argument("--asset", default="symsorter-Darwin-universal")
    add_write_flag(bump_symsorter)
    bump_symsorter.set_defaults(func=command_bump_symsorter)

    resolve_action = subparsers.add_parser("resolve-action", help="resolve a GitHub Action tag to a commit SHA")
    resolve_action.add_argument("--repo", required=True, help="owner/repo")
    resolve_action.add_argument("--tag", required=True)
    resolve_action.set_defaults(func=command_resolve_action)

    apple_root_sha = subparsers.add_parser("apple-root-sha", help="print the current Apple root certificate hash")
    apple_root_sha.add_argument("--details", action="store_true", help="also print certificate details with openssl")
    apple_root_sha.set_defaults(func=command_apple_root_sha)

    bump_apple_root = subparsers.add_parser("bump-apple-root", help="update the Apple root certificate hash")
    add_write_flag(bump_apple_root)
    bump_apple_root.set_defaults(func=command_bump_apple_root)

    verify_parser = subparsers.add_parser("verify", help="verify GitHub Actions dependency pins and policy")
    verify_parser.add_argument("--network", action="store_true", help="also verify network-resolved hashes")
    verify_parser.add_argument(
        "--large-downloads", action="store_true", help="with --network, also download large ipsw archives"
    )
    verify_parser.set_defaults(func=command_verify)

    show = subparsers.add_parser("show", help="print the GitHub Actions dependency manifest")
    show.set_defaults(func=command_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except GhaDependencyError as error:
        eprint(str(error))
        return 1
    except subprocess.CalledProcessError as error:
        eprint(f"command failed with exit code {error.returncode}: {shlex.join(error.cmd)}")
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
