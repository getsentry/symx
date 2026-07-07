import json
import logging
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeGuard
from urllib.parse import urlparse

import sentry_sdk
from pydantic import BaseModel, ConfigDict, Field

from symx.diagnostics import (
    decode_subprocess_output,
    directory_data,
    format_command,
    subprocess_result_data,
    truncate_text,
)
from symx.model import Arch
from symx.tools import dyld_split, symsort
from symx.ipsw.model import IpswPlatform

logger = logging.getLogger(__name__)

_MOUNT_POINT_RE = re.compile(r".*Press Ctrl\+C to unmount '(.*)'")
_EXTRACTED_MOUNT_ARTIFACT_RE = re.compile(r"^Extracted (.+?)(?: from .*)?$")
_SYMSORTER_SORTED_DEBUG_FILES_RE = re.compile(r"^Sorted (\d+) debug files$")
_SYMSORTER_CREATED_SOURCE_BUNDLES_RE = re.compile(r"^Created (\d+) source bundles$")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_LEADING_IPSW_GLYPH_RE = re.compile(r"^\s*[•⨯]\s*")

_SYS_MOUNT_CLEANUP_TIMEOUT_SECONDS = 60
_RESERVED_PROCESSING_DIR_NAMES = frozenset({"split_out", "symbols", "sys_mount"})
_ERROR_SUMMARY_MARKERS = (
    "error",
    "failed",
    "invalid",
    "not found",
    "unable",
    "unknown flag",
    "must specify",
)
_IGNORED_IPSW_HELP_PREFIXES = ("Usage:", "Aliases:", "Examples:", "Flags:", "Global Flags:")
_FCS_KEY_URL_LABEL = "[com.apple.wkms.fcs-key-url]:"
_VENDORED_PEM_DB = Path(__file__).resolve().parent / "data" / "fcs-keys.json"
_AEA_KEY_MAX_ATTEMPTS = 3
_AEA_KEY_RETRY_DELAY_SECONDS = 2
_TRANSIENT_FCS_KEY_ERROR_MARKERS = (
    "connection reset",
    "connection refused",
    "dial tcp",
    "i/o timeout",
    "network is unreachable",
    "no such host",
    "server misbehaving",
    "temporary failure",
    "tls handshake timeout",
)
_VERSION_MAJOR_RE = re.compile(r"^(\d+)")
_MACOS_ROSETTA_DSC_MIN_MAJOR_VERSION = 27
_ROSETTA_DSC_SOURCES = (
    ("x86_64", Path("System/Library/dyld/dyld_shared_cache_x86_64")),
    ("x86_64_x86Support", Path("System/x86Support/System/Library/dyld/dyld_shared_cache_x86_64")),
)


@dataclass(frozen=True)
class DirectoryTreeStats:
    path: str
    exists: bool
    is_dir: bool
    file_count: int = 0
    total_file_size_bytes: int = 0
    directory_count: int = 0
    symlink_count: int = 0
    other_entry_count: int = 0
    error_count: int = 0

    def to_span_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "path": self.path,
            "exists": self.exists,
            "is_dir": self.is_dir,
        }
        if not self.is_dir:
            return data

        data.update(
            {
                "file_count": self.file_count,
                "total_file_size_bytes": self.total_file_size_bytes,
                "directory_count": self.directory_count,
                "symlink_count": self.symlink_count,
                "other_entry_count": self.other_entry_count,
            }
        )
        if self.error_count:
            data["error_count"] = self.error_count
        return data


@dataclass(frozen=True)
class DirectoryTreeStatsDelta:
    file_count_delta: int
    total_file_size_bytes_delta: int
    directory_count_delta: int
    symlink_count_delta: int
    other_entry_count_delta: int

    def to_span_data(self) -> dict[str, int]:
        return {
            "file_count_delta": self.file_count_delta,
            "total_file_size_bytes_delta": self.total_file_size_bytes_delta,
            "directory_count_delta": self.directory_count_delta,
            "symlink_count_delta": self.symlink_count_delta,
            "other_entry_count_delta": self.other_entry_count_delta,
        }


@dataclass(frozen=True)
class SysImageMount:
    process: subprocess.Popen[str]
    requested_mount_point: Path
    active_mount_point: Path | None
    extracted_artifact_paths: list[Path]
    error: str | None = None


@dataclass(frozen=True)
class IpswProductMetadata:
    version: str | None = None
    build: str | None = None
    devices: tuple[str, ...] = ()


@dataclass(frozen=True)
class IpswDmgPaths:
    system: str | None
    filesystem: str | None
    rosetta: str | None
    selected: str | None

    def to_span_data(self) -> dict[str, str | None]:
        return {
            "system": self.system,
            "filesystem": self.filesystem,
            "rosetta": self.rosetta,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class DscSplitSource:
    label: str
    artifact: Path


class _IpswBuildManifestModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class IpswManifestComponentInfo(_IpswBuildManifestModel):
    path: str | None = Field(None, alias="Path")


class IpswManifestComponent(_IpswBuildManifestModel):
    info: IpswManifestComponentInfo | None = Field(None, alias="Info")


class IpswBuildIdentityManifest(_IpswBuildManifestModel):
    cryptex_system_os: IpswManifestComponent | None = Field(None, alias="Cryptex1,SystemOS")
    cryptex_rosetta_os: IpswManifestComponent | None = Field(None, alias="Cryptex1,RosettaOS")
    os: IpswManifestComponent | None = Field(None, alias="OS")


class IpswBuildIdentityInfo(_IpswBuildManifestModel):
    variant: str | None = Field(None, alias="Variant")


class IpswBuildIdentity(_IpswBuildManifestModel):
    manifest: IpswBuildIdentityManifest | None = Field(None, alias="Manifest")
    info: IpswBuildIdentityInfo | None = Field(None, alias="Info")


class IpswBuildManifest(_IpswBuildManifestModel):
    product_version: str | None = Field(None, alias="ProductVersion")
    product_build_version: str | None = Field(None, alias="ProductBuildVersion")
    supported_product_types: tuple[str, ...] = Field(default_factory=tuple, alias="SupportedProductTypes")
    build_identities: tuple[IpswBuildIdentity, ...] | None = Field(None, alias="BuildIdentities")


@dataclass(frozen=True)
class IpswExtractionRequest:
    platform: IpswPlatform
    ipsw_path: Path
    processing_dir: Path
    version: str | None = None
    build: str | None = None
    devices: tuple[str, ...] = ()

    @classmethod
    def from_local_ipsw(
        cls,
        platform: IpswPlatform,
        ipsw_path: Path,
        processing_dir: Path,
    ) -> "IpswExtractionRequest":
        try:
            metadata = inspect_ipsw_product_metadata(ipsw_path)
        except Exception as error:
            logger.warning("Failed to inspect IPSW product metadata for %s: %s", ipsw_path.name, error)
            metadata = IpswProductMetadata()

        return cls(
            platform=platform,
            ipsw_path=ipsw_path,
            processing_dir=processing_dir,
            version=metadata.version,
            build=metadata.build,
            devices=metadata.devices,
        )


def vendored_ipsw_pem_db_path() -> Path | None:
    """Return the vendored IPSW AEA PEM DB if it is available in the checkout/package."""
    if _VENDORED_PEM_DB.is_file():
        return _VENDORED_PEM_DB
    return None


def _ipsw_command_data(
    command: list[str],
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    directories: list[Path],
) -> dict[str, object]:
    return {
        "command": format_command(command),
        "stdout": truncate_text(stdout),
        "stderr": truncate_text(stderr),
        "stderr_summary": _summarize_ipsw_stderr(stderr),
        "directories": [directory_data(directory) for directory in directories],
    }


def _output_line_count(output: str | bytes | None) -> int:
    text = decode_subprocess_output(output)
    if not text:
        return 0
    return len(text.splitlines())


def _directory_tree_stats(directory: Path) -> DirectoryTreeStats:
    exists = directory.exists()
    is_dir = directory.is_dir()
    if not is_dir:
        return DirectoryTreeStats(path=str(directory), exists=exists, is_dir=is_dir)

    file_count = 0
    total_file_size_bytes = 0
    directory_count = 0
    symlink_count = 0
    other_entry_count = 0
    error_count = 0
    seen_files: set[tuple[int, int]] = set()
    stack = [directory]

    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            error_count += 1
            continue

        for entry in entries:
            try:
                entry_stat = entry.lstat()
            except OSError:
                error_count += 1
                continue

            mode = entry_stat.st_mode
            if stat.S_ISDIR(mode):
                directory_count += 1
                stack.append(entry)
            elif stat.S_ISREG(mode):
                file_key = (entry_stat.st_dev, entry_stat.st_ino)
                if file_key in seen_files:
                    continue
                seen_files.add(file_key)
                file_count += 1
                total_file_size_bytes += entry_stat.st_size
            elif stat.S_ISLNK(mode):
                symlink_count += 1
            else:
                other_entry_count += 1

    return DirectoryTreeStats(
        path=str(directory),
        exists=exists,
        is_dir=is_dir,
        file_count=file_count,
        total_file_size_bytes=total_file_size_bytes,
        directory_count=directory_count,
        symlink_count=symlink_count,
        other_entry_count=other_entry_count,
        error_count=error_count,
    )


def _directory_tree_delta(before: DirectoryTreeStats, after: DirectoryTreeStats) -> DirectoryTreeStatsDelta:
    return DirectoryTreeStatsDelta(
        file_count_delta=after.file_count - before.file_count,
        total_file_size_bytes_delta=after.total_file_size_bytes - before.total_file_size_bytes,
        directory_count_delta=after.directory_count - before.directory_count,
        symlink_count_delta=after.symlink_count - before.symlink_count,
        other_entry_count_delta=after.other_entry_count - before.other_entry_count,
    )


def _parse_symsorter_summary(stdout: str | bytes | None, stderr: str | bytes | None) -> dict[str, int]:
    summary: dict[str, int] = {}
    for line in decode_subprocess_output(stdout).splitlines():
        if match := _SYMSORTER_SORTED_DEBUG_FILES_RE.match(line):
            summary["sorted_debug_files"] = int(match.group(1))
        elif match := _SYMSORTER_CREATED_SOURCE_BUNDLES_RE.match(line):
            summary["created_source_bundles"] = int(match.group(1))

    stderr_text = decode_subprocess_output(stderr)
    duplicate_warning_count = stderr_text.count("already exists")
    if duplicate_warning_count:
        summary["duplicate_debug_file_warnings"] = duplicate_warning_count

    return summary


def _record_mount_process_output(
    span: Any,
    stdout_key: str,
    stderr_key: str,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> None:
    span.set_data(stdout_key, truncate_text(stdout))
    span.set_data(stderr_key, truncate_text(stderr))
    span.set_data(f"{stdout_key}_line_count", _output_line_count(stdout))
    span.set_data(f"{stderr_key}_line_count", _output_line_count(stderr))


def _terminate_sys_mount_process(mount_proc: subprocess.Popen[str], span: Any) -> None:
    returncode_before_cleanup = mount_proc.poll()
    span.set_data("returncode_before_cleanup", returncode_before_cleanup)

    if returncode_before_cleanup is not None:
        span.set_data("already_exited", True)
        return

    span.set_data("sent_signal", "SIGINT")
    mount_proc.send_signal(signal.SIGINT)
    try:
        # Drain remaining output while waiting, so a full stdout pipe cannot block unmount teardown.
        remaining_stdout, remaining_stderr = mount_proc.communicate(timeout=_SYS_MOUNT_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        span.set_status("internal_error")
        span.set_data("sigint_timeout_seconds", _SYS_MOUNT_CLEANUP_TIMEOUT_SECONDS)
        span.set_data("timed_out_after_sigint", True)
        _record_mount_process_output(
            span,
            "partial_stdout_after_sigint",
            "partial_stderr_after_sigint",
            error.output,
            error.stderr,
        )
        logger.warning("Mount process did not terminate after SIGINT, killing it")
        mount_proc.kill()
        span.set_data("killed", True)
        remaining_stdout, remaining_stderr = mount_proc.communicate()
        span.set_data("returncode_after_kill", mount_proc.returncode)
        _record_mount_process_output(
            span,
            "remaining_stdout_after_kill",
            "remaining_stderr_after_kill",
            remaining_stdout,
            remaining_stderr,
        )
        return

    span.set_data("timed_out_after_sigint", False)
    span.set_data("returncode_after_sigint", mount_proc.returncode)
    _record_mount_process_output(
        span,
        "remaining_stdout_after_sigint",
        "remaining_stderr_after_sigint",
        remaining_stdout,
        remaining_stderr,
    )


def _cleanup_sys_mount_directory(mount_point: Path, span: Any) -> None:
    span.set_data("mount_point_exists_before_directory_cleanup", mount_point.exists())
    if mount_point.exists():
        logger.warning("Mount point still exists after unmount, attempting cleanup", extra={"mount_point": mount_point})
        try:
            shutil.rmtree(mount_point)
        except Exception as e:
            span.set_status("internal_error")
            logger.error("Failed to clean up mount point", extra={"mount_point": mount_point, "exception": e})
    span.set_data("mount_point_exists_after_directory_cleanup", mount_point.exists())


def _cleanup_sys_image_mount(mount: SysImageMount) -> None:
    with sentry_sdk.start_span(op="subprocess.ipsw_mount_cleanup", name="Unmount sys image") as span:
        span.set_data("requested_mount_point", str(mount.requested_mount_point))
        if mount.active_mount_point is not None:
            span.set_data("active_mount_point", str(mount.active_mount_point))

        _terminate_sys_mount_process(mount.process, span)
        _cleanup_sys_mount_directory(mount.requested_mount_point, span)

    for extracted_mount_artifact_path in mount.extracted_artifact_paths:
        _cleanup_mount_artifact(extracted_mount_artifact_path)
        if extracted_mount_artifact_path.suffix == ".aea":
            _cleanup_mount_artifact(
                extracted_mount_artifact_path.with_suffix(""),
                description="decrypted DMG artifact left by ipsw mount",
            )


def _is_object_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


def _manifest_component_path(component: IpswManifestComponent | None) -> str | None:
    if component is None or component.info is None:
        return None
    return component.info.path or None


def _read_ipsw_build_manifest(ipsw_path: Path) -> IpswBuildManifest:
    with zipfile.ZipFile(ipsw_path) as archive:
        build_manifest_obj: object = plistlib.loads(archive.read("BuildManifest.plist"))

    return IpswBuildManifest.model_validate(build_manifest_obj)


def inspect_ipsw_product_metadata(ipsw_path: Path) -> IpswProductMetadata:
    build_manifest = _read_ipsw_build_manifest(ipsw_path)
    return IpswProductMetadata(
        version=build_manifest.product_version,
        build=build_manifest.product_build_version,
        devices=build_manifest.supported_product_types,
    )


def inspect_ipsw_dmg_paths(ipsw_path: Path) -> IpswDmgPaths:
    build_manifest = _read_ipsw_build_manifest(ipsw_path)
    if build_manifest.build_identities is None:
        raise ValueError(f"BuildManifest.plist has no BuildIdentities list in {ipsw_path}")

    system_dmg: str | None = None
    rosetta_dmg: str | None = None
    filesystem_dmgs: list[str] = []

    for build_identity in build_manifest.build_identities:
        manifest = build_identity.manifest
        if manifest is None:
            continue

        if system_dmg is None:
            system_dmg = _manifest_component_path(manifest.cryptex_system_os)
        if rosetta_dmg is None:
            rosetta_dmg = _manifest_component_path(manifest.cryptex_rosetta_os)

        filesystem_dmg = _manifest_component_path(manifest.os)
        if filesystem_dmg is None:
            continue

        variant = build_identity.info.variant if build_identity.info is not None else None
        if variant is not None and "Recovery" in variant:
            continue

        if filesystem_dmg not in filesystem_dmgs:
            filesystem_dmgs.append(filesystem_dmg)

    filesystem_dmg = filesystem_dmgs[0] if len(filesystem_dmgs) == 1 else None
    selected_dmg = system_dmg if system_dmg is not None else filesystem_dmg

    return IpswDmgPaths(system=system_dmg, filesystem=filesystem_dmg, rosetta=rosetta_dmg, selected=selected_dmg)


def _extract_ipsw_member(ipsw_path: Path, member_name: str, output_path: Path) -> Path:
    with zipfile.ZipFile(ipsw_path) as archive:
        with archive.open(member_name) as src, output_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    return output_path


def _parse_fcs_key_url(output: str | bytes | None) -> str | None:
    text = _strip_ansi(decode_subprocess_output(output))
    if not text:
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if line != _FCS_KEY_URL_LABEL:
            continue
        if idx + 1 < len(lines):
            return lines[idx + 1]

    return None


def _fcs_key_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    key_id = Path(parsed.path).name
    return key_id or None


def _is_transient_fcs_key_error(stderr: str | bytes | None) -> bool:
    text = decode_subprocess_output(stderr).lower()
    if "failed to connect to fcs-key url" not in text:
        return False
    return any(marker in text for marker in _TRANSIENT_FCS_KEY_ERROR_MARKERS)


def _compress_directory(directory: Path) -> Path:
    archive_path = directory.parent / f"{directory.name}.tar.zst"

    with subprocess.Popen(
        ["tar", "-cf", "-", "-C", str(directory.parent), str(directory.name)], stdout=subprocess.PIPE
    ) as tar_proc:
        with subprocess.Popen(
            ["zstd", "-", "-o", str(archive_path)],
            stdin=tar_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as zstd_proc:
            if tar_proc.stdout:
                tar_proc.stdout.close()  # Allow tar_proc to receive SIGPIPE if zstd_proc exits
            _, stderr = zstd_proc.communicate()

            if zstd_proc.returncode != 0:
                error_msg = stderr.decode("utf-8") if stderr else "Unknown error"
                raise IpswExtractError(f"zstd compression failed: {error_msg}")

    if tar_proc.returncode != 0:
        raise IpswExtractError(f"tar archiving failed with return code {tar_proc.returncode}")

    # Remove the original directory after compression
    shutil.rmtree(directory)

    return archive_path


def _decompress_archive(archive_path: Path, target_dir: Path) -> None:
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Use subprocess to pipe zstd output to tar for decompression
    with subprocess.Popen(["zstd", "-d", str(archive_path), "-c"], stdout=subprocess.PIPE) as zstd_proc:
        with subprocess.Popen(
            ["tar", "-xf", "-", "-C", str(target_dir.parent)],
            stdin=zstd_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as tar_proc:
            if zstd_proc.stdout:
                zstd_proc.stdout.close()
            _, stderr = tar_proc.communicate()

            if tar_proc.returncode != 0:
                error_msg = stderr.decode("utf-8") if stderr else "Unknown error"
                raise IpswExtractError(f"tar extraction failed: {error_msg}")

    if zstd_proc.returncode != 0:
        raise IpswExtractError(f"zstd decompression failed with return code {zstd_proc.returncode}")


def extract_ipsw(request: IpswExtractionRequest) -> Path:
    return _IpswExtractionRun(request).run()


class _IpswExtractionRun:
    def __init__(self, request: IpswExtractionRequest):
        self.request = request
        self.bundle_id = generate_bundle_id(request.ipsw_path.name)
        self.prefix = map_platform_to_prefix(request.platform)
        self.platform = request.platform
        self.macos_dsc_architectures = (
            _macos_dsc_architectures(request.version) if request.platform == IpswPlatform.MACOS else ()
        )
        if not request.processing_dir.is_dir():
            raise ValueError(f"IPSW processing path is expected to be a directory: {request.processing_dir}")
        self.processing_dir = request.processing_dir
        _log_directory_contents(self.processing_dir)

        if not request.ipsw_path.is_file():
            raise ValueError(f"IPSW path is expected to be a file: {request.ipsw_path}")

        self.ipsw_path = request.ipsw_path
        self._aea_preflight_complete = False

    def _ipsw_aea_preflight(self) -> None:
        if self._aea_preflight_complete:
            return

        with sentry_sdk.start_span(op="ipsw.preflight.aea", name="IPSW AEA preflight") as span:
            span.set_data("ipsw_path", str(self.ipsw_path))

            try:
                dmg_paths = inspect_ipsw_dmg_paths(self.ipsw_path)
            except Exception as error:
                span.set_data("preflight_error", f"failed to inspect IPSW DMG metadata: {error}")
                logger.warning("Failed to inspect IPSW AEA metadata for %s: %s", self.ipsw_path.name, error)
                self._aea_preflight_complete = True
                return

            span.set_data("ipsw_dmg_paths", dmg_paths.to_span_data())
            selected_dmg = dmg_paths.selected
            if selected_dmg is None or not selected_dmg.endswith(".aea"):
                self._aea_preflight_complete = True
                return

            probe_data: dict[str, object] = {
                "selected_dmg": selected_dmg,
                "system_dmg": dmg_paths.system,
                "filesystem_dmg": dmg_paths.filesystem,
            }
            pem_db_path = vendored_ipsw_pem_db_path()
            if pem_db_path is not None:
                probe_data["pem_db_path"] = str(pem_db_path)

            with tempfile.TemporaryDirectory(suffix="_ipsw_aea_probe") as tmpdir:
                temp_dir = Path(tmpdir)
                extracted_aea = temp_dir / Path(selected_dmg).name

                try:
                    _extract_ipsw_member(self.ipsw_path, selected_dmg, extracted_aea)
                except Exception as error:
                    span.set_data("preflight_error", f"failed to extract AEA member {selected_dmg}: {error}")
                    logger.warning(
                        "Failed to extract IPSW AEA preflight member %s from %s: %s",
                        selected_dmg,
                        self.ipsw_path.name,
                        error,
                    )
                    self._aea_preflight_complete = True
                    return

                info_command = ["ipsw", "--no-color", "fw", "aea", "--info", str(extracted_aea)]
                info_result = subprocess.run(info_command, capture_output=True)
                info_command_data = _ipsw_command_data(info_command, info_result.stdout, info_result.stderr, [temp_dir])
                info_command_data.update(probe_data)
                span.set_data("aea_info", info_command_data)
                if info_result.returncode != 0:
                    span.set_status("internal_error")
                    summary = _summarize_ipsw_stderr(info_result.stderr) or "ipsw fw aea --info failed"
                    raise IpswExtractError(f"IPSW AEA preflight failed for {self.ipsw_path}: {summary}")

                fcs_key_url = _parse_fcs_key_url(info_result.stdout)
                fcs_key_id = _fcs_key_id_from_url(fcs_key_url)
                vendored_db_hit = False
                if fcs_key_id is not None:
                    vendored_db_hit = fcs_key_id in _vendored_ipsw_pem_db_keys()

                probe_data["fcs_key_url"] = fcs_key_url
                probe_data["fcs_key_id"] = fcs_key_id
                probe_data["vendored_db_hit"] = vendored_db_hit
                span.set_data("aea_probe", probe_data)

                key_command = ["ipsw", "--no-color", "fw", "aea", "--key"]
                if pem_db_path is not None:
                    key_command.extend(["--pem-db", str(pem_db_path)])
                key_command.append(str(extracted_aea))

                key_result: subprocess.CompletedProcess[bytes] | None = None
                key_attempt_data: list[dict[str, object]] = []
                for attempt in range(1, _AEA_KEY_MAX_ATTEMPTS + 1):
                    key_result = subprocess.run(key_command, capture_output=True)
                    key_command_data = _ipsw_command_data(key_command, key_result.stdout, key_result.stderr, [temp_dir])
                    key_command_data.update(probe_data)
                    key_command_data["attempt"] = attempt
                    key_attempt_data.append(key_command_data)

                    if key_result.returncode == 0:
                        break
                    if attempt == _AEA_KEY_MAX_ATTEMPTS or not _is_transient_fcs_key_error(key_result.stderr):
                        break

                    logger.warning(
                        "Transient IPSW AEA FCS-key lookup failed for %s (attempt %d/%d), retrying",
                        self.ipsw_path.name,
                        attempt,
                        _AEA_KEY_MAX_ATTEMPTS,
                    )
                    time.sleep(_AEA_KEY_RETRY_DELAY_SECONDS * attempt)

                span.set_data("aea_key_probe", key_attempt_data[0] if len(key_attempt_data) == 1 else key_attempt_data)
                if key_result is None or key_result.returncode != 0:
                    span.set_status("internal_error")
                    summary = (
                        _summarize_ipsw_stderr(key_result.stderr) if key_result is not None else None
                    ) or "ipsw fw aea --key failed"
                    raise IpswExtractError(
                        f"IPSW AEA preflight failed for {self.ipsw_path}: "
                        f"selected_dmg={selected_dmg}; system_dmg={dmg_paths.system}; "
                        f"filesystem_dmg={dmg_paths.filesystem}; fcs_key={fcs_key_id or '<unknown>'}; "
                        f"vendored_db_hit={vendored_db_hit}; {summary}"
                    )

        self._aea_preflight_complete = True

    def symbols_dir(self) -> Path:
        return self.processing_dir / "symbols"

    def _ipsw_extract_dsc(self, arch: Arch | None = None) -> Path:
        arch_label = str(arch) if arch else "default"
        with sentry_sdk.start_span(
            op="subprocess.ipsw_extract",
            name=f"ipsw extract DSC ({arch_label})",
        ) as span:
            span.set_data("arch", arch_label)
            span.set_data("ipsw_path", str(self.ipsw_path))

            command: list[str] = [
                "ipsw",
                "extract",
                str(self.ipsw_path),
                "-d",
                "-o",
                str(self.processing_dir),
                "-V",
            ]

            pem_db_path = vendored_ipsw_pem_db_path()
            if pem_db_path is not None:
                command.extend(["--pem-db", str(pem_db_path)])

            if arch is not None:
                command.append("-a")
                command.append(str(arch))

            stdout: bytes | None = None
            stderr: bytes | None = None
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                try:
                    # IPSW extraction is typically finished in a couple of minutes. Everything beyond 20 minutes is probably
                    # stuck because the dmg mounter asks for a password or something similar.
                    stdout, stderr = process.communicate(timeout=(60 * 20))
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    span.set_data("ipsw_extract", _ipsw_command_data(command, stdout, stderr, [self.processing_dir]))
                    span.set_status("deadline_exceeded")
                    raise IpswExtractTimeoutError(f"ipsw extract timed out for {self.ipsw_path} ({arch_label})")

                span.set_data("ipsw_extract", _ipsw_command_data(command, stdout, stderr, [self.processing_dir]))
                if process.returncode != 0:
                    span.set_status("internal_error")
                    stderr_summary = _summarize_ipsw_stderr(stderr)
                    detail = f": {stderr_summary}" if stderr_summary else ""
                    raise IpswExtractError(
                        f"ipsw extract failed for {self.ipsw_path} ({arch_label}) with exit code {process.returncode}{detail}"
                    )

            _log_directory_contents(self.processing_dir)
            extract_dir = find_extraction_dir(self.processing_dir)
            if extract_dir is None:
                span.set_data("processing_dir", directory_data(self.processing_dir))
                span.set_status("internal_error")
                raise IpswExtractError(
                    f"ipsw extract produced no dyld_shared_cache extraction directory for {self.ipsw_path} ({arch_label})"
                )

            span.set_data("extract_dir", directory_data(extract_dir))
            span.set_data("extract_output_tree", _directory_tree_stats(extract_dir).to_span_data())
            return extract_dir

    def run(self) -> Path:
        self._ipsw_aea_preflight()
        with sentry_sdk.start_span(op="ipsw.extract.sys_image", name="Symsort sys image"):
            self._symsort_sys_image()
        with sentry_sdk.start_span(op="ipsw.extract.dsc", name=f"Extract+split+symsort DSC ({self.platform})"):
            self._symsort_dsc()
        _log_directory_contents(self.symbols_dir())

        return self.symbols_dir()

    def _symsort_dsc(self) -> None:
        split_dir = self.processing_dir / "split_out"
        if self.platform == IpswPlatform.MACOS:
            compressed_archives: list[tuple[Path, str]] = []

            for arch in self.macos_dsc_architectures:
                logger.info("Extracting and processing DSC for %s", arch)
                with sentry_sdk.start_span(op="ipsw.extract.dsc_arch", name=f"Extract+split DSC {arch}") as arch_span:
                    arch_span.set_data("arch", str(arch))
                    arch_span.set_data("version", self.request.version)
                    arch_span.set_data("build", self.request.build)

                    rosetta_dmg_path = self._rosetta_dmg_path_for_x86_64_dsc() if arch == Arch.X86_64 else None
                    if rosetta_dmg_path is not None:
                        split_labels = self._split_rosetta_dscs()
                        split_dir = self.processing_dir / "split_out"
                    else:
                        extract_dir = self._ipsw_extract_dsc(arch)
                        _log_directory_contents(extract_dir)

                        split_dir = self._ipsw_split(extract_dir, arch)
                        _log_directory_contents(split_dir)
                        split_labels = [str(arch)]

                    for split_label in split_labels:
                        arch_split_dir = split_dir / split_label
                        if arch_split_dir.exists():
                            with sentry_sdk.start_span(op="ipsw.compress", name=f"Compress {split_label} split"):
                                archive_path = _compress_directory(arch_split_dir)
                            compressed_archives.append((archive_path, split_label))
                            logger.info("Finished compressing %s split to %s", split_label, archive_path)

            # Delete IPSW file now that all required DSC architectures are extracted and compressed
            if self.ipsw_path.exists():
                logger.info("Deleting IPSW file to save space: %s", self.ipsw_path)
                self.ipsw_path.unlink()

            # Decompress all archives before final symsort processing
            for archive_path, split_label in compressed_archives:
                with sentry_sdk.start_span(op="ipsw.decompress", name=f"Decompress {split_label} split"):
                    _decompress_archive(archive_path, split_dir / split_label)
                archive_path.unlink()  # Remove the compressed archive after extraction

            # We accumulate each architecture as a sub-dir in split_dir and let symsorter process them together
        else:
            extract_dir = self._ipsw_extract_dsc()
            _log_directory_contents(extract_dir)

            # Delete IPSW also in the non-macOS path since this is our contract to the caller
            if self.ipsw_path.exists():
                logger.info("Deleting IPSW file to save space: %s", self.ipsw_path)
                self.ipsw_path.unlink()

            split_dir = self._ipsw_split(extract_dir)
            _log_directory_contents(split_dir)
        self._symsort(split_dir)
        # we have very limited space on the GHA runners, so get rid of processed input data
        shutil.rmtree(split_dir)

    def _sys_mount_point(self) -> Path:
        return self.processing_dir / "sys_mount"

    def _symsort_sys_image(self) -> None:
        with self._mounted_sys_image() as active_mount_point:
            self._symsort(active_mount_point, ignore_errors=True, record_input_tree=False)

    @contextmanager
    def _mounted_sys_image(self) -> Generator[Path, None, None]:
        mount = self._prepare_sys_image_mount()
        try:
            if mount.error is not None:
                raise IpswExtractError(mount.error)
            if mount.active_mount_point is None:
                raise IpswExtractError(f"Could not determine sys image mount point for {self.ipsw_path}")

            yield mount.active_mount_point
        finally:
            _cleanup_sys_image_mount(mount)

    def _prepare_sys_image_mount(self) -> SysImageMount:
        mount_point = self._sys_mount_point()
        if mount_point.exists():
            logger.warning("Removing stale sys image mount point before mount", extra={"mount_point": mount_point})
            if mount_point.is_dir():
                shutil.rmtree(mount_point)
            else:
                mount_point.unlink()

        mount_output_lines: list[str] = []
        extracted_mount_artifact_paths: list[Path] = []
        active_mount_point: Path | None = None
        mount_error: str | None = None

        with sentry_sdk.start_span(op="subprocess.ipsw_mount", name="Mount sys image") as mount_span:
            mount_span.set_data("requested_mount_point", str(mount_point))
            mount_command = [
                "ipsw",
                "mount",
                "sys",
                str(self.ipsw_path),
                "-V",
                "--mount-point",
                str(mount_point),
            ]
            pem_db_path = vendored_ipsw_pem_db_path()
            if pem_db_path is not None:
                mount_command.extend(["--pem-db", str(pem_db_path)])

            mount_proc = subprocess.Popen(
                mount_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
            )

            # Read only until ipsw reports the mount point. Cleanup drains the remaining output later.
            while mount_proc.stdout:
                line = mount_proc.stdout.readline()
                if not line:
                    break
                mount_output_lines.append(line.rstrip())

                extracted_mount_artifact_path = _parse_extracted_mount_artifact_path(line)
                if (
                    extracted_mount_artifact_path is not None
                    and extracted_mount_artifact_path not in extracted_mount_artifact_paths
                ):
                    extracted_mount_artifact_paths.append(extracted_mount_artifact_path)

                mount_point_match = _MOUNT_POINT_RE.match(line)
                if not mount_point_match:
                    continue

                active_mount_point = Path(mount_point_match.group(1))
                break

            mount_span.set_data("mount_output_line_count", len(mount_output_lines))
            if mount_output_lines:
                mount_span.set_data("mount_output_preview", mount_output_lines[:20])

            if active_mount_point:
                mount_span.set_data("mount_point", str(active_mount_point))
            else:
                mount_output = "\n".join(mount_output_lines)
                mount_summary = _summarize_ipsw_stderr(mount_output)
                mount_span.set_data("mount_output_summary", mount_summary)
                mount_span.set_status("internal_error")
                if mount_summary:
                    mount_error = f"ipsw mount sys failed for {self.ipsw_path}: {mount_summary}"
                else:
                    mount_error = f"Could not determine sys image mount point for {self.ipsw_path}"

        return SysImageMount(
            process=mount_proc,
            requested_mount_point=mount_point,
            active_mount_point=active_mount_point,
            extracted_artifact_paths=extracted_mount_artifact_paths,
            error=mount_error,
        )

    def _rosetta_dmg_path(self) -> str | None:
        return inspect_ipsw_dmg_paths(self.ipsw_path).rosetta

    def _rosetta_dmg_path_for_x86_64_dsc(self) -> str | None:
        if not _macos_x86_64_dsc_requires_rosetta(self.request.version):
            return None

        try:
            rosetta_dmg_path = self._rosetta_dmg_path()
        except Exception as error:
            raise IpswExtractError(
                f"Cannot determine RosettaOS DMG path required for macOS {self.request.version} x86_64 DSC: {error}"
            ) from error

        if rosetta_dmg_path is None:
            raise IpswExtractError(
                f"macOS {self.request.version} x86_64 DSC requires Cryptex1,RosettaOS in BuildManifest"
            )

        if rosetta_dmg_path.endswith(".aea"):
            raise IpswExtractError(
                f"macOS {self.request.version} x86_64 DSC requires RosettaOS DMG, "
                f"but it is AEA encrypted and cannot be mounted directly: {rosetta_dmg_path}"
            )

        return rosetta_dmg_path

    def _split_rosetta_dscs(self) -> list[str]:
        rosetta_dmg_member = self._rosetta_dmg_path()
        if rosetta_dmg_member is None:
            raise IpswExtractError(f"IPSW BuildManifest has no RosettaOS DMG for {self.ipsw_path}")
        if rosetta_dmg_member.endswith(".aea"):
            raise IpswExtractError(
                f"RosettaOS DMG is AEA encrypted and cannot be mounted directly: {rosetta_dmg_member}"
            )

        with sentry_sdk.start_span(op="ipsw.extract.rosetta_dsc", name="Extract+split RosettaOS DSC") as span:
            span.set_data("rosetta_dmg_member", rosetta_dmg_member)
            with tempfile.TemporaryDirectory(suffix="_ipsw_rosetta_dmg") as tmpdir:
                temp_dir = Path(tmpdir)
                rosetta_dmg = temp_dir / Path(rosetta_dmg_member).name
                _extract_ipsw_member(self.ipsw_path, rosetta_dmg_member, rosetta_dmg)
                span.set_data("rosetta_dmg", directory_data(rosetta_dmg))

                with self._mounted_readonly_dmg(rosetta_dmg) as mount_point:
                    sources = _find_rosetta_dsc_sources(mount_point)
                    span.set_data("rosetta_dsc_sources", {source.label: str(source.artifact) for source in sources})
                    if not sources:
                        span.set_status("internal_error")
                        raise IpswExtractError(
                            f"RosettaOS DMG contains no supported dyld_shared_cache files: {rosetta_dmg}"
                        )

                    split_labels: list[str] = []
                    for source in sources:
                        self._ipsw_split_dsc_file(source.artifact, source.label, split_label=source.label)
                        split_labels.append(source.label)

                    return split_labels

    @contextmanager
    def _mounted_readonly_dmg(self, dmg: Path) -> Generator[Path, None, None]:
        mount_point = Path(tempfile.mkdtemp(prefix=f"{dmg.stem}_mount_"))
        with sentry_sdk.start_span(op="subprocess.hdiutil_mount", name=f"Mount DMG {dmg.name}") as span:
            command = ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount_point), str(dmg)]
            result = subprocess.run(command, capture_output=True)
            span.set_data("hdiutil_attach", _ipsw_command_data(command, result.stdout, result.stderr, [mount_point]))
            if result.returncode != 0:
                span.set_status("internal_error")
                shutil.rmtree(mount_point, ignore_errors=True)
                raise IpswExtractError(f"hdiutil attach failed for {dmg}")

        try:
            yield mount_point
        finally:
            with sentry_sdk.start_span(op="subprocess.hdiutil_detach", name=f"Unmount DMG {dmg.name}") as span:
                command = ["hdiutil", "detach", str(mount_point)]
                result = subprocess.run(command, capture_output=True)
                span.set_data(
                    "hdiutil_detach", _ipsw_command_data(command, result.stdout, result.stderr, [mount_point])
                )
                if result.returncode != 0:
                    span.set_status("internal_error")
                    logger.warning("hdiutil detach failed for %s", mount_point)
            if mount_point.exists():
                shutil.rmtree(mount_point, ignore_errors=True)

    def _ipsw_split(self, extract_dir: Path, arch: Arch | None = None) -> Path:
        dsc_root_file = None
        for item in extract_dir.iterdir():
            if item.is_file() and not item.suffix:  # check if it is a file and has no extension
                dsc_root_file = item
                break

        if dsc_root_file is None:
            raise IpswExtractError(f"Failed to find dyld_shared_cache root-file in {extract_dir}")

        arch_label = str(arch) if arch is not None else "default"
        return self._ipsw_split_dsc_file(
            dsc_root_file,
            arch_label,
            split_label=str(arch) if arch is not None else None,
            cleanup_dir=extract_dir,
        )

    def _ipsw_split_dsc_file(
        self,
        dsc_root_file: Path,
        arch_label: str,
        split_label: str | None = None,
        cleanup_dir: Path | None = None,
    ) -> Path:
        with sentry_sdk.start_span(op="ipsw.dyld_split", name=f"Split IPSW DSC ({arch_label})") as span:
            split_dir = self.processing_dir / "split_out"
            if split_label is not None:
                # each arch/cache gets its own sub-dir, so that the split-dir can be symsorted in one go
                split_dir_arch = split_dir / split_label
            else:
                split_dir_arch = split_dir

            if cleanup_dir is not None:
                span.set_data("extract_dir", directory_data(cleanup_dir))
            span.set_data("dsc_root_file", str(dsc_root_file))
            result = dyld_split(dsc_root_file, split_dir_arch)
            span.set_data("dyld_split", subprocess_result_data(result))
            span.set_data("split_dir", directory_data(split_dir_arch))
            span.set_data("split_output_tree", _directory_tree_stats(split_dir_arch).to_span_data())

            if result.returncode != 0:
                span.set_status("internal_error")
                raise IpswExtractError(f"ipsw dyld split failed for {dsc_root_file}")

            if cleanup_dir is not None:
                # we have very limited space on the GHA runners, so get rid of processed input data
                shutil.rmtree(cleanup_dir)

            return split_dir

    def _symsort(self, split_dir: Path, ignore_errors: bool = False, record_input_tree: bool = True) -> None:
        output_dir = self.symbols_dir()
        logger.info("Symsorting %s -> %s", split_dir, output_dir)

        with sentry_sdk.start_span(op="ipsw.symsort", name=f"Symsort IPSW {self.bundle_id}") as span:
            span.set_data("bundle_id", self.bundle_id)
            span.set_data("split_dir", directory_data(split_dir))
            if record_input_tree:
                span.set_data("input_tree", _directory_tree_stats(split_dir).to_span_data())

            output_tree_before = _directory_tree_stats(output_dir)
            span.set_data("output_dir", directory_data(output_dir))

            result = symsort(output_dir, self.prefix, self.bundle_id, split_dir, ignore_errors)
            span.set_data("symsort", subprocess_result_data(result))
            span.set_data("symsorter_summary", _parse_symsorter_summary(result.stdout, result.stderr))

            output_tree_after = _directory_tree_stats(output_dir)
            span.set_data(
                "output_tree_delta", _directory_tree_delta(output_tree_before, output_tree_after).to_span_data()
            )
            span.set_data("output_tree_total_after", output_tree_after.to_span_data())

            if result.returncode != 0:
                span.set_status("internal_error")
                raise IpswExtractError(f"Symsorter failed for bundle {self.bundle_id}")


class IpswExtractError(Exception):
    pass


class IpswExtractTimeoutError(IpswExtractError, TimeoutError):
    pass


def _find_rosetta_dsc_sources(mount_point: Path) -> list[DscSplitSource]:
    sources: list[DscSplitSource] = []
    for label, relative_path in _ROSETTA_DSC_SOURCES:
        artifact = mount_point / relative_path
        if artifact.is_file():
            sources.append(DscSplitSource(label=label, artifact=artifact))
    return sources


def find_extraction_dir(processing_dir: Path) -> Path | None:
    """
    Find the DSC extraction directory in the processing directory.

    After ipsw extract, the DSC ends up in a directory with an unpredictable name.
    We find it by looking for the only directory that isn't one of symx's reserved
    processing directories.
    """
    for item in processing_dir.iterdir():
        if item.is_dir() and item.name not in _RESERVED_PROCESSING_DIR_NAMES:
            logger.info(
                "Found IPSW dyld extraction directory",
                extra={"directory": item},
            )
            return item
    return None


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _normalize_ipsw_output_line(line: str) -> str:
    return _LEADING_IPSW_GLYPH_RE.sub("", line).strip()


def _parse_extracted_mount_artifact_path(line: str) -> Path | None:
    normalized_line = _normalize_ipsw_output_line(_strip_ansi(line))
    match = _EXTRACTED_MOUNT_ARTIFACT_RE.match(normalized_line)
    if not match:
        return None
    return Path(match.group(1))


def _cleanup_mount_artifact(path: Path, description: str = "mount artifact left by ipsw mount") -> None:
    if not path.exists():
        return

    logger.warning("Removing %s", description, extra={"artifact_path": path})
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except Exception as e:
        logger.error(
            "Failed to remove %s",
            description,
            extra={"artifact_path": path, "exception": e},
        )


@lru_cache(maxsize=1)
def _vendored_ipsw_pem_db_keys() -> frozenset[str]:
    pem_db_path = vendored_ipsw_pem_db_path()
    if pem_db_path is None:
        return frozenset()

    with pem_db_path.open() as handle:
        raw_data_obj: object = json.load(handle)

    if not _is_object_mapping(raw_data_obj):
        raise ValueError(f"Vendored IPSW PEM DB must be a JSON object: {pem_db_path}")

    return frozenset(str(key) for key in raw_data_obj)


def _summarize_ipsw_stderr(stderr: str | bytes | None) -> str | None:
    text = decode_subprocess_output(stderr)
    if not text:
        return None

    stripped = _strip_ansi(text)
    lines = [_normalize_ipsw_output_line(line) for line in stripped.splitlines() if line.strip()]
    if not lines:
        return None

    for line in reversed(lines):
        if line.startswith(_IGNORED_IPSW_HELP_PREFIXES):
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in _ERROR_SUMMARY_MARKERS):
            return line

    return None


def _version_major(version: str | None) -> int | None:
    if version is None:
        return None

    version_match = _VERSION_MAJOR_RE.match(version)
    if version_match is None:
        return None
    return int(version_match.group(1))


def _macos_x86_64_dsc_requires_rosetta(version: str | None) -> bool:
    major_version = _version_major(version)
    return major_version is not None and major_version >= _MACOS_ROSETTA_DSC_MIN_MAJOR_VERSION


def _macos_dsc_architectures(version: str | None) -> list[Arch]:
    major_version = _version_major(version)
    if major_version is None:
        version_label = "<missing>" if version is None else repr(version)
        raise IpswExtractError(
            f"Cannot determine required macOS DSC architectures: missing or unparseable macOS version {version_label}"
        )

    return [Arch.ARM64E, Arch.X86_64]


def generate_bundle_id(file_name: str) -> str:
    """Generate bundle ID from IPSW filename."""
    # Remove .ipsw extension and replace commas with underscores
    clean_name = file_name[:-5].replace(",", "_")
    return f"ipsw_{clean_name}"


def _log_directory_contents(directory: Path) -> None:
    if not directory.is_dir():
        return
    dir_contents = "\n".join(str(item.name) for item in directory.iterdir())
    logger.info("Contents of directory.", extra={"directory": directory, "contents": dir_contents})


def map_platform_to_prefix(ipsw_platform: IpswPlatform) -> str:
    # IPSWs differentiate between iPadOS and iOS while OTA doesn't, so we put them in the same prefix
    if ipsw_platform == IpswPlatform.IPADOS:
        prefix_platform = IpswPlatform.IOS
    else:
        prefix_platform = ipsw_platform

    # the symbols store prefixes are all lower-case
    return str(prefix_platform).lower()
