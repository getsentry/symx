import json
import logging
import plistlib
import re
import shutil
import signal
import subprocess
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import sentry_sdk

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

mount_point_re = re.compile(r".*Press Ctrl\+C to unmount '(.*)'")


_VENDORED_PEM_DB = Path(__file__).resolve().parent / "data" / "fcs-keys.json"


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


def _manifest_component_path(component: object) -> str | None:
    if not isinstance(component, dict):
        return None

    component_dict = cast(dict[str, Any], component)
    info = component_dict.get("Info")
    if not isinstance(info, dict):
        return None

    info_dict = cast(dict[str, Any], info)
    path = info_dict.get("Path")
    if isinstance(path, str) and path:
        return path
    return None


def inspect_ipsw_dmg_paths(ipsw_path: Path) -> dict[str, str | None]:
    with zipfile.ZipFile(ipsw_path) as archive:
        build_manifest_obj = plistlib.loads(archive.read("BuildManifest.plist"))

    if not isinstance(build_manifest_obj, dict):
        raise ValueError(f"Unexpected BuildManifest.plist structure in {ipsw_path}")

    build_manifest = cast(dict[str, Any], build_manifest_obj)
    build_identities_obj = build_manifest.get("BuildIdentities")
    if not isinstance(build_identities_obj, list):
        raise ValueError(f"BuildManifest.plist has no BuildIdentities list in {ipsw_path}")

    build_identities = cast(list[object], build_identities_obj)
    system_dmg: str | None = None
    filesystem_dmgs: list[str] = []

    for build_identity_obj in build_identities:
        if not isinstance(build_identity_obj, dict):
            continue

        build_identity = cast(dict[str, Any], build_identity_obj)
        manifest_obj = build_identity.get("Manifest")
        if not isinstance(manifest_obj, dict):
            continue

        manifest = cast(dict[str, Any], manifest_obj)
        if system_dmg is None:
            system_dmg = _manifest_component_path(manifest.get("Cryptex1,SystemOS"))

        filesystem_dmg = _manifest_component_path(manifest.get("OS"))
        if filesystem_dmg is None:
            continue

        info_obj = build_identity.get("Info")
        variant = None
        if isinstance(info_obj, dict):
            info = cast(dict[str, Any], info_obj)
            variant_obj = info.get("Variant")
            if isinstance(variant_obj, str):
                variant = variant_obj
        if variant is not None and "Recovery" in variant:
            continue

        if filesystem_dmg not in filesystem_dmgs:
            filesystem_dmgs.append(filesystem_dmg)

    filesystem_dmg = filesystem_dmgs[0] if len(filesystem_dmgs) == 1 else None
    selected_dmg = system_dmg if system_dmg is not None else filesystem_dmg

    return {
        "system": system_dmg,
        "filesystem": filesystem_dmg,
        "selected": selected_dmg,
    }


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


# TODO: there is good chance that we should split the extractor into separate classes based on at least platform
#   and provide a factory function that instantiates the right extractor class using artifact and maybe source.
class IpswExtractor:
    def __init__(
        self,
        platform: IpswPlatform,
        file_name: str,
        processing_dir: Path,
        ipsw_path: Path,
    ):
        self.bundle_id = generate_bundle_id(file_name)
        self.prefix = map_platform_to_prefix(platform)
        self.platform = platform
        if not processing_dir.is_dir():
            raise ValueError(f"IPSW path is expected to be a directory: {processing_dir}")
        self.processing_dir = processing_dir
        _log_directory_contents(self.processing_dir)

        if not ipsw_path.is_file():
            raise ValueError(f"IPSW path is expected to be a file: {ipsw_path}")

        self.ipsw_path = ipsw_path
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

            span.set_data("ipsw_dmg_paths", dmg_paths)
            selected_dmg = dmg_paths.get("selected")
            if not isinstance(selected_dmg, str) or not selected_dmg.endswith(".aea"):
                self._aea_preflight_complete = True
                return

            probe_data: dict[str, object] = {
                "selected_dmg": selected_dmg,
                "system_dmg": dmg_paths.get("system"),
                "filesystem_dmg": dmg_paths.get("filesystem"),
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

                key_result = subprocess.run(key_command, capture_output=True)
                key_command_data = _ipsw_command_data(key_command, key_result.stdout, key_result.stderr, [temp_dir])
                key_command_data.update(probe_data)
                span.set_data("aea_key_probe", key_command_data)
                if key_result.returncode != 0:
                    span.set_status("internal_error")
                    summary = _summarize_ipsw_stderr(key_result.stderr) or "ipsw fw aea --key failed"
                    raise IpswExtractError(
                        f"IPSW AEA preflight failed for {self.ipsw_path}: "
                        f"selected_dmg={selected_dmg}; system_dmg={dmg_paths.get('system')}; "
                        f"filesystem_dmg={dmg_paths.get('filesystem')}; fcs_key={fcs_key_id or '<unknown>'}; "
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
            # all macOS IPSWs have dyld_shared_caches for both architectures
            compressed_archives: list[tuple[Path, str]] = []

            for arch in [Arch.ARM64E, Arch.X86_64]:
                logger.info("Extracting and processing DSC for %s", arch)
                with sentry_sdk.start_span(op="ipsw.extract.dsc_arch", name=f"Extract+split DSC {arch}") as arch_span:
                    arch_span.set_data("arch", str(arch))

                    extract_dir = self._ipsw_extract_dsc(arch)
                    _log_directory_contents(extract_dir)

                    split_dir = self._ipsw_split(extract_dir, arch)
                    _log_directory_contents(split_dir)

                    arch_split_dir = split_dir / str(arch)
                    if arch_split_dir.exists():
                        with sentry_sdk.start_span(op="ipsw.compress", name=f"Compress {arch} split"):
                            archive_path = _compress_directory(arch_split_dir)
                        compressed_archives.append((archive_path, arch))
                        logger.info("Finished compressing %s split to %s", arch, archive_path)

            # Delete IPSW file now that both architectures are extracted and compressed
            if self.ipsw_path.exists():
                logger.info("Deleting IPSW file to save space: %s", self.ipsw_path)
                self.ipsw_path.unlink()

            # Decompress all archives before final symsort processing
            for archive_path, arch in compressed_archives:
                with sentry_sdk.start_span(op="ipsw.decompress", name=f"Decompress {arch} split"):
                    _decompress_archive(archive_path, split_dir / str(arch))
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

    def _symsort_sys_image(self) -> None:
        # mount the sys image (the process waits for sigint)
        mount_output_lines: list[str] = []
        with sentry_sdk.start_span(op="subprocess.ipsw_mount", name="Mount sys image") as mount_span:
            mount_command = ["ipsw", "mount", "sys", str(self.ipsw_path), "-V"]
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

            # read mount-output until we get the mount-point
            mount_point = None
            while mount_proc.stdout:
                line = mount_proc.stdout.readline()
                if not line:
                    break
                mount_output_lines.append(line.rstrip())
                mount_point_match = mount_point_re.match(line)
                if not mount_point_match:
                    continue

                mount_point = Path(mount_point_match.group(1))
                break

            if mount_point:
                mount_span.set_data("mount_point", str(mount_point))
            elif mount_output_lines:
                mount_output = "\n".join(mount_output_lines)
                mount_summary = _summarize_ipsw_stderr(mount_output)
                mount_span.set_data("mount_output_preview", mount_output_lines[:20])
                mount_span.set_data("mount_output_summary", mount_summary)
                if mount_summary:
                    logger.warning(
                        "Could not determine sys image mount point for %s: %s", self.ipsw_path.name, mount_summary
                    )
                else:
                    logger.warning("Could not determine sys image mount point for %s", self.ipsw_path.name)

        try:
            # symsort the entire sys mount-point
            if mount_point:
                self._symsort(mount_point, ignore_errors=True)
        finally:
            # SIGINT the mount process and wait for it to unmount
            if mount_proc.poll() is None:
                mount_proc.send_signal(signal.SIGINT)
                try:
                    # Wait for process to cleanly unmount (timeout after 60 seconds)
                    mount_proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    logger.warning("Mount process did not terminate after SIGINT, killing it")
                    mount_proc.kill()
                    mount_proc.wait()

            # Clean up mount point directory if it still exists (in case unmount failed)
            if mount_point and mount_point.exists():
                logger.warning(
                    "Mount point still exists after unmount, attempting cleanup", extra={"mount_point": mount_point}
                )
                try:
                    shutil.rmtree(mount_point)
                except Exception as e:
                    logger.error("Failed to clean up mount point", extra={"mount_point": mount_point, "exception": e})

    def _ipsw_split(self, extract_dir: Path, arch: Arch | None = None) -> Path:
        arch_label = str(arch) if arch is not None else "default"
        with sentry_sdk.start_span(op="ipsw.dyld_split", name=f"Split IPSW DSC ({arch_label})") as span:
            dsc_root_file = None
            for item in extract_dir.iterdir():
                if item.is_file() and not item.suffix:  # check if it is a file and has no extension
                    dsc_root_file = item
                    break

            span.set_data("extract_dir", directory_data(extract_dir))
            if dsc_root_file is None:
                span.set_status("internal_error")
                raise IpswExtractError(f"Failed to find dyld_shared_cache root-file in {extract_dir}")

            split_dir = self.processing_dir / "split_out"
            if arch is not None:
                # each arch gets its own sub-dir, so that the split-dir can be symsorted in one go
                split_dir_arch = split_dir / str(arch)
            else:
                split_dir_arch = split_dir

            span.set_data("dsc_root_file", str(dsc_root_file))
            result = dyld_split(dsc_root_file, split_dir_arch)
            span.set_data("dyld_split", subprocess_result_data(result))
            span.set_data("split_dir", directory_data(split_dir_arch))

            if result.returncode != 0:
                span.set_status("internal_error")
                raise IpswExtractError(f"ipsw dyld split failed for {dsc_root_file}")

            # we have very limited space on the GHA runners, so get rid of processed input data
            shutil.rmtree(extract_dir)

            return split_dir

    def _symsort(self, split_dir: Path, ignore_errors: bool = False) -> None:
        output_dir = self.symbols_dir()
        logger.info("Symsorting %s -> %s", split_dir, output_dir)

        with sentry_sdk.start_span(op="ipsw.symsort", name=f"Symsort IPSW {self.bundle_id}") as span:
            span.set_data("bundle_id", self.bundle_id)
            span.set_data("split_dir", directory_data(split_dir))
            span.set_data("output_dir", directory_data(output_dir))

            result = symsort(output_dir, self.prefix, self.bundle_id, split_dir, ignore_errors)
            span.set_data("symsort", subprocess_result_data(result))

            if result.returncode != 0:
                span.set_status("internal_error")
                raise IpswExtractError(f"Symsorter failed for bundle {self.bundle_id}")


class IpswExtractError(Exception):
    pass


class IpswExtractTimeoutError(IpswExtractError, TimeoutError):
    pass


def find_extraction_dir(processing_dir: Path) -> Path | None:
    """
    Find the DSC extraction directory in the processing directory.

    After ipsw extract, the DSC ends up in a directory with an unpredictable name.
    We find it by looking for the only directory that isn't "split_out" or "symbols".
    """
    for item in processing_dir.iterdir():
        if item.is_dir() and item.name not in ["split_out", "symbols"]:
            logger.info(
                "Found IPSW dyld extraction directory",
                extra={"directory": item},
            )
            return item
    return None


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_LEADING_IPSW_GLYPH_RE = re.compile(r"^\s*[•⨯]\s*")
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
        raw_data_obj = json.load(handle)

    if not isinstance(raw_data_obj, dict):
        raise ValueError(f"Vendored IPSW PEM DB must be a JSON object: {pem_db_path}")

    raw_data = cast(dict[object, object], raw_data_obj)
    return frozenset(str(key) for key in raw_data)


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
