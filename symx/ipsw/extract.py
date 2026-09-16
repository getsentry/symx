import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import zipfile
from collections import deque
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import IO, TypeGuard
from urllib.parse import urlparse

import sentry_sdk

from symx.diagnostics import (
    decode_subprocess_output,
    directory_data,
    format_command,
    subprocess_result_data,
    truncate_text,
)
from symx.directory_archive import (
    DirectoryArchiveError,
    compress_directory,
    decompress_archive,
)
from symx.model import Arch
from symx.ipsw.errors import IpswExtractError, IpswExtractTimeoutError, IpswMountCleanupError
from symx.ipsw.image_plan import (
    IpswDscAttemptRequest,
    IpswImageTarget,
    build_extraction_plan,
    macos_dsc_architectures,
    read_build_manifest,
)
from symx.ipsw.mounts import image_workspace
from symx.tools import dyld_split, symsort
from symx.ipsw.materialization import (
    IpswDscMaterializationAttempt,
    IpswDscMaterialized,
    IpswDscNotPresent,
    IpswDscUnavailable,
    IpswDscUnavailableReason,
)
from symx.ipsw.model import IpswPlatform

logger = logging.getLogger(__name__)

_MOUNT_POINT_RE = re.compile(r".*Press Ctrl\+C to unmount '(.*)'")
_SYMSORTER_SORTED_DEBUG_FILES_RE = re.compile(r"^Sorted (\d+) debug files$")
_SYMSORTER_CREATED_SOURCE_BUNDLES_RE = re.compile(r"^Created (\d+) source bundles$")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_LEADING_IPSW_GLYPH_RE = re.compile(r"^\s*[•⨯]\s*")

_SYS_MOUNT_CLEANUP_TIMEOUT_SECONDS = 60
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
class IpswProductMetadata:
    version: str | None = None
    build: str | None = None
    devices: tuple[str, ...] = ()


@dataclass(frozen=True)
class DscSplitSource:
    label: str
    artifact: Path


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


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    # All callers start a private session. Reap children too, including an
    # hdiutil process interrupted before ipsw installed its SIGINT handler.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@contextmanager
def _owned_process(
    command: list[str], *, stdout: int | IO[str], stderr: int, cwd: Path, env: dict[str, str]
) -> Generator[subprocess.Popen[bytes], None, None]:
    process = subprocess.Popen(command, start_new_session=True, stdout=stdout, stderr=stderr, cwd=cwd, env=env)
    primary: BaseException | None = None

    try:
        yield process
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)

                try:
                    process.communicate(timeout=_SYS_MOUNT_CLEANUP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    logger.warning("IPSW process did not stop after SIGINT; killing its process group")

            _kill_process_group(process)
            process.communicate(timeout=_SYS_MOUNT_CLEANUP_TIMEOUT_SECONDS)
        except BaseException as cleanup_error:
            error = IpswMountCleanupError(f"Cannot stop IPSW process; retaining its workspace: {cleanup_error}")

            if primary is not None:
                error.add_note(f"Process cleanup error: {cleanup_error}")
                raise error from primary
            raise error from cleanup_error


def _wait_for_sys_mount(
    process: subprocess.Popen[bytes], log_path: Path, mount_point: Path, image: IpswImageTarget
) -> list[str]:
    """Wait for the expected mount point and return a bounded output preview.

    The caller owns the process and workspace, including cleanup on failure.
    Reading a regular file does not wait for ipsw to emit more output.
    """
    deadline = time.monotonic() + 20 * 60
    recent: deque[str] = deque(maxlen=20)
    with log_path.open() as log_input:
        while True:
            if time.monotonic() >= deadline:
                raise IpswExtractTimeoutError(f"ipsw mount sys timed out for {image.member}")

            line = log_input.readline()
            if not line:
                if process.poll() is not None:
                    detail = _summarize_ipsw_stderr("\n".join(recent)) or "no mount readiness reported"
                    raise IpswExtractError(f"ipsw mount sys failed for {image.member}: {detail}")
                time.sleep(0.05)
                continue

            recent.append(line.rstrip())
            match = _MOUNT_POINT_RE.match(_strip_ansi(line))
            if not match:
                continue

            active = Path(match[1])
            if active.resolve() != mount_point.resolve():
                raise IpswExtractError(f"ipsw reported an unowned mount point: {active}")
            return list(recent)


def _is_object_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


def inspect_ipsw_product_metadata(ipsw_path: Path) -> IpswProductMetadata:
    build_manifest = read_build_manifest(ipsw_path)
    return IpswProductMetadata(
        version=build_manifest.product_version,
        build=build_manifest.product_build_version,
        devices=build_manifest.supported_product_types,
    )


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
    try:
        return compress_directory(directory)
    except DirectoryArchiveError as error:
        raise IpswExtractError(str(error)) from error


def _decompress_archive(archive_path: Path, target_dir: Path) -> None:
    try:
        decompress_archive(archive_path, target_dir)
    except DirectoryArchiveError as error:
        raise IpswExtractError(str(error)) from error


def extract_ipsw(request: IpswExtractionRequest) -> Path:
    return _IpswExtractionRun(request).run()


class _IpswExtractionRun:
    def __init__(self, request: IpswExtractionRequest):
        # Subprocesses run in private working directories, never the caller's
        # cwd. Make paths absolute without dereferencing the input's final
        # symlink: consuming that path must never unlink a different target.
        request = replace(
            request, ipsw_path=request.ipsw_path.absolute(), processing_dir=request.processing_dir.resolve()
        )
        self.request = request
        self.bundle_id = generate_bundle_id(request.ipsw_path.name)
        self.prefix = map_platform_to_prefix(request.platform)
        self.platform = request.platform

        if request.platform == IpswPlatform.MACOS:
            macos_dsc_architectures(request.version)

        if not request.processing_dir.is_dir():
            raise ValueError(f"IPSW processing path is expected to be a directory: {request.processing_dir}")

        self.processing_dir = request.processing_dir
        _log_directory_contents(self.processing_dir)

        if not request.ipsw_path.is_file():
            raise ValueError(f"IPSW path is expected to be a file: {request.ipsw_path}")

        self.ipsw_path = request.ipsw_path
        self._aea_preflight_complete: set[str] = set()

    def _ipsw_aea_preflight(self, image: IpswImageTarget) -> None:
        selected_dmg = image.member
        if selected_dmg in self._aea_preflight_complete or not selected_dmg.endswith(".aea"):
            return

        with sentry_sdk.start_span(op="ipsw.preflight.aea", name="IPSW AEA preflight") as span:
            span.set_data("ipsw_path", str(self.ipsw_path))
            probe_data: dict[str, object] = {"selected_dmg": selected_dmg, "image_kind": str(image.kind)}
            pem_db_path = vendored_ipsw_pem_db_path()
            if pem_db_path is not None:
                probe_data["pem_db_path"] = str(pem_db_path)

            with tempfile.TemporaryDirectory(suffix="_ipsw_aea_probe") as tmpdir:
                temp_dir = Path(tmpdir)
                extracted_aea = temp_dir / Path(selected_dmg).name

                _extract_ipsw_member(self.ipsw_path, selected_dmg, extracted_aea)

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
                        f"selected_dmg={selected_dmg}; fcs_key={fcs_key_id or '<unknown>'}; "
                        f"vendored_db_hit={vendored_db_hit}; {summary}"
                    )

        self._aea_preflight_complete.add(selected_dmg)

    def symbols_dir(self) -> Path:
        return self.processing_dir / "symbols"

    def _ipsw_extract_dsc(self, attempt: IpswDscAttemptRequest) -> IpswDscMaterializationAttempt:
        arch = attempt.arch
        arch_label = str(arch) if arch else "default"
        output_dir = attempt.output_dir
        output_dir.mkdir()
        temp_dir = attempt.work_dir / "tmp"
        temp_dir.mkdir()

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
                str(output_dir),
                "-V",
                *attempt.image.selector_args,
            ]

            pem_db_path = vendored_ipsw_pem_db_path()
            if pem_db_path is not None:
                command.extend(["--pem-db", str(pem_db_path)])

            if arch is not None:
                command.append("-a")
                command.append(str(arch))

            stdout: bytes | None = None
            stderr: bytes | None = None
            with _owned_process(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=attempt.work_dir,
                env={**os.environ, "TMPDIR": str(temp_dir)},
            ) as process:
                try:
                    # IPSW extraction is typically finished in a couple of minutes. Everything beyond 20 minutes is probably
                    # stuck because the dmg mounter asks for a password or something similar.
                    stdout, stderr = process.communicate(timeout=(60 * 20))
                except subprocess.TimeoutExpired:
                    _kill_process_group(process)
                    stdout, stderr = process.communicate(timeout=_SYS_MOUNT_CLEANUP_TIMEOUT_SECONDS)
                    span.set_data("ipsw_extract", _ipsw_command_data(command, stdout, stderr, [output_dir]))
                    span.set_status("deadline_exceeded")
                    raise IpswExtractTimeoutError(f"ipsw extract timed out for {self.ipsw_path} ({arch_label})")

                span.set_data("ipsw_extract", _ipsw_command_data(command, stdout, stderr, [output_dir]))
                if process.returncode != 0:
                    stderr_text = decode_subprocess_output(stderr).strip()
                    if (
                        arch is not None
                        and "no dyld_shared_cache files found matching the specified archs" in stderr_text
                    ):
                        span.set_data("materialization_outcome", "not_present")
                        return IpswDscNotPresent(arch=arch, message=stderr_text)

                    span.set_status("internal_error")
                    stderr_summary = _summarize_ipsw_stderr(stderr)
                    detail = f": {stderr_summary}" if stderr_summary else ""
                    return IpswDscUnavailable(
                        arch=arch,
                        reason=IpswDscUnavailableReason.INVOCATION_FAILED,
                        message=(
                            f"ipsw extract failed for {self.ipsw_path} ({arch_label}) "
                            f"with exit code {process.returncode}{detail}"
                        ),
                    )

            extract_dir = output_dir
            if not any(extract_dir.iterdir()):
                span.set_data("processing_dir", directory_data(output_dir))
                span.set_status("internal_error")
                return IpswDscUnavailable(
                    arch=arch,
                    reason=IpswDscUnavailableReason.NO_EXTRACT_DIR,
                    message=(
                        f"ipsw extract produced no dyld_shared_cache extraction directory "
                        f"for {self.ipsw_path} ({arch_label})"
                    ),
                )

            span.set_data("extract_dir", directory_data(extract_dir))
            span.set_data("extract_output_tree", _directory_tree_stats(extract_dir).to_span_data())
            return IpswDscMaterialized(arch=arch, extract_dir=extract_dir)

    def run(self) -> Path:
        plan = build_extraction_plan(self.request)
        for image in (*plan.system_images, *plan.rosetta_images):
            logger.info(
                "IPSW image planned kind=%s member=%s selector=%s products=%d %s boards=%d %s",
                image.kind,
                image.member,
                image.selector,
                len(image.products),
                image.products[:8],
                len(image.boards),
                image.boards[:8],
            )

        if plan.unmatched_devices:
            logger.info(
                "IPSW unmatched source devices: count=%d examples=%s",
                len(plan.unmatched_devices),
                plan.unmatched_devices[:8],
            )

        archives: list[tuple[Path, Path]] = []
        for image in plan.system_images:
            self._ipsw_aea_preflight(image)
            self._symsort_sys_image(image)
            image_archives: list[tuple[Path, Path]] = []

            for attempt in plan.attempts_for(image):
                image_archives.extend(self._materialize_and_archive(attempt))

            if not image_archives:
                raise IpswExtractError(
                    f"IPSW image {image.member}: none of the requested architectures has a usable DSC"
                )

            archives.extend(image_archives)
        for image in plan.rosetta_images:
            for attempt in plan.attempts_for(image):
                if attempt.arch == Arch.X86_64:
                    archives.extend(self._split_rosetta_dscs(attempt))
                else:
                    archives.extend(self._materialize_and_archive(attempt))

        # Source ownership, not image/architecture ownership. No IPSW consumer
        # remains; release the input before restoring one split batch at a time.
        logger.info("Deleting consumed IPSW file to save space: %s", self.ipsw_path)
        self.ipsw_path.unlink()
        for archive, output in archives:
            _decompress_archive(archive, output)
            archive.unlink()
            self._symsort(output)
            shutil.rmtree(output)

        for name in ("split_out", "dsc", "mounts"):
            directory = self.processing_dir / name
            if directory.exists():
                shutil.rmtree(directory)

        return self.symbols_dir()

    def _materialize_and_archive(self, attempt: IpswDscAttemptRequest) -> list[tuple[Path, Path]]:
        with sentry_sdk.start_span(op="ipsw.extract.dsc_arch", name=f"{attempt.image.member} / {attempt.arch}") as span:
            span.set_data("image", attempt.image.member)
            span.set_data("selector", attempt.image.selector)
            span.set_data("architecture", str(attempt.arch))
            with image_workspace(attempt.work_dir):
                match self._ipsw_extract_dsc(attempt):
                    case IpswDscNotPresent(message=message):
                        if attempt.arch is None:
                            raise IpswExtractError(
                                "unfiltered IPSW DSC materialization returned an architecture absence"
                            )

                        span.set_data("outcome", "not_present")
                        logger.info(
                            "IPSW DSC absent image=%s arch=%s: %s",
                            attempt.image.member,
                            attempt.arch,
                            _summarize_ipsw_stderr(message) or truncate_text(message),
                        )
                        return []
                    case IpswDscUnavailable(message=message):
                        raise IpswExtractError(message)
                    case IpswDscMaterialized(extract_dir=extract_dir):
                        span.set_data("outcome", "materialized")
                        return self._ipsw_split(extract_dir, attempt)

    def _archive_split(self, cache: Path, relative: Path, attempt: IpswDscAttemptRequest) -> tuple[Path, Path]:
        # Include the exact image, architecture AND cache location. Two images
        # can contain different UUIDs at the same /usr/lib/... path.
        output = self.processing_dir / "split_out" / attempt.image.key / str(attempt.arch or "default") / relative
        self._ipsw_split_dsc_file(cache, output)
        archive = _compress_directory(output)
        logger.info(
            "IPSW split archived image=%s arch=%s cache=%s archive=%s",
            attempt.image.member,
            attempt.arch,
            relative,
            archive,
        )
        return archive, output

    def _symsort_sys_image(self, image: IpswImageTarget) -> None:
        with self._mounted_sys_image(image) as active_mount_point:
            self._symsort(active_mount_point, ignore_errors=True, record_input_tree=False)

    @contextmanager
    def _mounted_sys_image(self, image: IpswImageTarget) -> Generator[Path, None, None]:
        root = self.processing_dir / "mounts" / image.key
        with image_workspace(root):
            mount_point = root / "mount"
            temp_dir = root / "tmp"
            temp_dir.mkdir()
            command = [
                "ipsw",
                "mount",
                "sys",
                str(self.ipsw_path),
                "-V",
                "--mount-point",
                str(mount_point),
                *image.selector_args,
            ]

            if pem_db := vendored_ipsw_pem_db_path():
                command.extend(["--pem-db", str(pem_db)])

            with sentry_sdk.start_span(op="subprocess.ipsw_mount", name=f"Mount {image.member}") as span:
                span.set_data("image", image.member)
                span.set_data("selector", image.selector)
                # A regular file avoids both pipe backpressure and unbounded
                # readline during acquisition. Polling has a readiness deadline.
                log_path = root / "mount.log"
                with (
                    log_path.open("w") as log_output,
                    _owned_process(
                        command,
                        stdout=log_output,
                        stderr=subprocess.STDOUT,
                        cwd=root,
                        env={**os.environ, "TMPDIR": str(temp_dir)},
                    ) as process,
                ):
                    preview = _wait_for_sys_mount(process, log_path, mount_point, image)
                    span.set_data("mount_output_preview", preview)
                    logger.info("IPSW mounted image=%s selector=%s mount=%s", image.member, image.selector, mount_point)
                    yield mount_point

    def _split_rosetta_dscs(self, attempt: IpswDscAttemptRequest) -> list[tuple[Path, Path]]:
        image = attempt.image
        with image_workspace(attempt.work_dir) as root:
            dmg = root / Path(image.member).name
            _extract_ipsw_member(self.ipsw_path, image.member, dmg)
            mount_point = root / "mount"
            mount_point.mkdir()

            command = ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount_point), str(dmg)]
            result = subprocess.run(command, capture_output=True, timeout=20 * 60)
            if result.returncode != 0:
                raise IpswExtractError(f"hdiutil attach failed for {image.member}")

            # The workspace owns cleanup, including partial acquisition failure.
            sources = _find_rosetta_dsc_sources(mount_point)
            if not sources:
                raise IpswExtractError(f"RosettaOS DMG contains no supported dyld_shared_cache files: {image.member}")

            return [
                self._archive_split(source.artifact, source.artifact.relative_to(mount_point), attempt)
                for source in sources
            ]

    def _ipsw_split(self, extract_dir: Path, attempt: IpswDscAttemptRequest) -> list[tuple[Path, Path]]:
        names = {f"dyld_shared_cache_{arch}" for arch in ((attempt.arch,) if attempt.arch else tuple(Arch))}
        primaries = sorted(
            path
            for path in extract_dir.rglob("dyld_shared_cache_*")
            if path.name in names and path.is_file() and not path.is_symlink()
        )
        if not primaries:
            raise IpswExtractError(f"Failed to find dyld_shared_cache root-file in {extract_dir}")

        return [self._archive_split(path, path.relative_to(extract_dir), attempt) for path in primaries]

    def _ipsw_split_dsc_file(self, dsc_root_file: Path, output: Path) -> None:
        with sentry_sdk.start_span(op="ipsw.dyld_split", name=f"Split IPSW DSC {dsc_root_file.name}") as span:
            span.set_data("dsc_root_file", str(dsc_root_file))
            result = dyld_split(dsc_root_file, output)
            span.set_data("dyld_split", subprocess_result_data(result))
            span.set_data("split_output_tree", _directory_tree_stats(output).to_span_data())
            logger.info("IPSW split cache=%s output=%s returncode=%s", dsc_root_file, output, result.returncode)

            if result.returncode != 0:
                span.set_status("internal_error")
                raise IpswExtractError(f"ipsw dyld split failed for {dsc_root_file}")

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


def _find_rosetta_dsc_sources(mount_point: Path) -> list[DscSplitSource]:
    sources: list[DscSplitSource] = []
    for label, relative_path in _ROSETTA_DSC_SOURCES:
        artifact = mount_point / relative_path
        if artifact.is_file():
            sources.append(DscSplitSource(label=label, artifact=artifact))
    return sources


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _normalize_ipsw_output_line(line: str) -> str:
    return _LEADING_IPSW_GLYPH_RE.sub("", line).strip()


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
