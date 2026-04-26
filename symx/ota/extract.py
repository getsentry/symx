"""OTA extraction pipeline: DSC finding, splitting, DMG mount/patch, and symsort."""

import glob
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import TypedDict

import sentry_sdk

from symx.diagnostics import (
    DEFAULT_DIRECTORY_SAMPLE_ENTRIES,
    decode_subprocess_output,
    directory_data,
    subprocess_result_data,
    truncate_text,
)
from symx.model import Arch
from symx.fs import list_dirs_in, rmdir_if_exists
from symx.tools import dyld_split, symsort as common_symsort
from symx.ota.model import (
    DYLD_SHARED_CACHE,
    DSCSearchResult,
    DeltaOtaError,
    DscSplitter,
    MountInfo,
    OtaExtractError,
    RecoveryOtaError,
    UnsupportedOtaPayloadError,
)

logger = logging.getLogger(__name__)

PAYLOAD_FILE_NAME_RE = re.compile(r"(^|.*/)payloadv2/payload\.\d+$")
DYLD_BOM_ENTRY_RE = re.compile(
    r"^(?:\./)?(?:System/DriverKit/)?System/Library/(?:dyld|Caches/com\.apple\.dyld)/dyld_shared_cache_[^\s/]+$"
)
DYLD_AA_INCLUDE_REGEX = r"(System/DriverKit/)?System/Library/(dyld|Caches/com\.apple\.dyld)/dyld_shared_cache_"
UNSUPPORTED_AA_ERROR_MARKERS = (
    "invalid/non-supported archive stream",
    "archive stream read error (header)",
)
MAX_AA_PROBE_OUTPUT_CHARS = 500


class PayloadAaProbeResult(TypedDict):
    payload: str
    returncode: int
    stdout: str | None
    stderr: str | None
    extracted_count: int
    unsupported_error: bool


def _extract_dirs_data(output_dir: Path) -> list[dict[str, object]]:
    if not output_dir.exists() or not output_dir.is_dir():
        return []

    extract_dirs = list_dirs_in(output_dir)
    results: list[dict[str, object]] = []
    for extract_dir in extract_dirs:
        data = directory_data(extract_dir, max_entries=DEFAULT_DIRECTORY_SAMPLE_ENTRIES)
        data["contains_dsc"] = _dir_contains_dsc(extract_dir)
        results.append(data)
    return results


def _should_probe_unsupported_payload(error: OtaExtractError) -> bool:
    message = str(error)
    return any(
        marker in message
        for marker in (
            f"Could not find {DYLD_SHARED_CACHE}",
            f"OTA extraction produced no {DYLD_SHARED_CACHE} files",
            f"Couldn't find any {DYLD_SHARED_CACHE} paths",
        )
    )


def _ota_is_zip_archive(artifact: Path) -> bool:
    try:
        return zipfile.is_zipfile(artifact)
    except OSError:
        return False


def _payload_entry_names(artifact: Path) -> list[str]:
    with zipfile.ZipFile(artifact) as archive:
        return [name for name in archive.namelist() if PAYLOAD_FILE_NAME_RE.search(name)]


def _read_post_bom_dsc_matches(artifact: Path) -> list[str]:
    with zipfile.ZipFile(artifact) as archive:
        post_bom_name = next((name for name in archive.namelist() if name.endswith("post.bom")), None)
        if post_bom_name is None:
            return []

        with tempfile.TemporaryDirectory(suffix="_ota_post_bom") as post_bom_tmp_dir:
            bom_path = Path(post_bom_tmp_dir) / Path(post_bom_name).name
            bom_path.write_bytes(archive.read(post_bom_name))
            result = subprocess.run(["lsbom", str(bom_path)], capture_output=True, text=True)

        if result.returncode != 0:
            return []

        matches: list[str] = []
        for line in result.stdout.splitlines():
            entry = line.split("\t", 1)[0].strip()
            if DYLD_BOM_ENTRY_RE.match(entry):
                matches.append(entry.removeprefix("./"))
        return matches


def _aa_result_indicates_unsupported_payload(stdout: str | bytes | None, stderr: str | bytes | None) -> bool:
    combined = "\n".join(
        part for part in (decode_subprocess_output(stdout), decode_subprocess_output(stderr)) if part
    ).lower()
    return any(marker in combined for marker in UNSUPPORTED_AA_ERROR_MARKERS)


def _probe_payload_with_aa(artifact: Path, payload_name: str, pattern: str) -> PayloadAaProbeResult:
    with tempfile.TemporaryDirectory(suffix="_ota_aa_probe") as aa_probe_dir:
        output_dir = Path(aa_probe_dir) / "out"
        output_dir.mkdir(parents=True, exist_ok=True)
        payload_input_path = Path(aa_probe_dir) / "payload_input.bin"

        with zipfile.ZipFile(artifact) as archive:
            with archive.open(payload_name) as payload_stream, payload_input_path.open("wb") as payload_input_file:
                shutil.copyfileobj(payload_stream, payload_input_file)

        with payload_input_path.open("rb") as payload_input_file:
            result = subprocess.run(
                ["aa", "extract", "-d", str(output_dir), "-include-regex", pattern],
                stdin=payload_input_file,
                capture_output=True,
            )

        extracted_count = sum(1 for path in output_dir.rglob("*") if path.is_file())
        return {
            "payload": payload_name,
            "returncode": result.returncode,
            "stdout": truncate_text(result.stdout, max_chars=MAX_AA_PROBE_OUTPUT_CHARS),
            "stderr": truncate_text(result.stderr, max_chars=MAX_AA_PROBE_OUTPUT_CHARS),
            "extracted_count": extracted_count,
            "unsupported_error": _aa_result_indicates_unsupported_payload(result.stdout, result.stderr),
        }


def _probe_unsupported_payload_format(artifact: Path) -> bool:
    with sentry_sdk.start_span(op="ota.extract.payload_probe", name="Probe unsupported OTA payload") as span:
        span.set_data("artifact", str(artifact))

        try:
            if not _ota_is_zip_archive(artifact):
                span.set_data("is_zip_archive", False)
                return False

            span.set_data("is_zip_archive", True)
            bom_matches = _read_post_bom_dsc_matches(artifact)
            span.set_data("post_bom_dsc_match_count", len(bom_matches))
            span.set_data("post_bom_dsc_matches", bom_matches[:10])
            if not bom_matches:
                return False

            payload_names = _payload_entry_names(artifact)
            span.set_data("payload_probe_count", len(payload_names))
            if not payload_names:
                return False

            results = [
                _probe_payload_with_aa(artifact, payload_name, DYLD_AA_INCLUDE_REGEX) for payload_name in payload_names
            ]
            extracted_total = sum(result["extracted_count"] for result in results)
            unsupported_errors = sum(1 for result in results if result["unsupported_error"])
            other_errors = sum(1 for result in results if result["returncode"] != 0 and not result["unsupported_error"])
            span.set_data(
                "payload_probe_summary",
                {
                    "payloads_probed": len(results),
                    "extracted_total": extracted_total,
                    "unsupported_errors": unsupported_errors,
                    "other_errors": other_errors,
                },
            )
            span.set_data("payload_probe_results", results[:10])

            unsupported = extracted_total == 0 and unsupported_errors > 0 and other_errors == 0
            span.set_data("unsupported_payload_format", unsupported)
            return unsupported
        except (FileNotFoundError, OSError, ValueError, zipfile.BadZipFile) as exc:
            span.set_data("payload_probe_error", str(exc))
            return False


def parse_cryptex_patch_output(stderr: str) -> dict[str, Path]:
    """Parse ipsw ota patch stderr to extract DMG file mappings."""
    dmg_files: dict[str, Path] = {}
    for line in stderr.splitlines():
        match = re.search(r"Patching (.*) to (.*)", line)
        if match:
            dmg_files[match.group(1)] = Path(match.group(2))
    return dmg_files


def patch_cryptex_dmg(artifact: Path, output_dir: Path) -> dict[str, Path]:
    with sentry_sdk.start_span(op="subprocess.ipsw_ota_patch", name="Patch cryptex DMG"):
        result = subprocess.run(
            ["ipsw", "ota", "patch", str(artifact), "--output", str(output_dir)],
            capture_output=True,
        )
        if result.returncode == 0 and result.stderr:
            return parse_cryptex_patch_output(result.stderr.decode("utf-8"))
    return {}


def find_system_os_dmgs(search_dir: Path) -> list[Path]:
    result: list[Path] = []
    for artifact in glob.iglob(str(search_dir) + "/**/SystemOS/*.dmg", recursive=True):
        result.append(Path(artifact))
    return result


def parse_hdiutil_mount_output(cmd_output: str) -> MountInfo:
    # hdiutil output uses tabs as delimiters with space padding, handle both
    last_line = cmd_output.splitlines().pop()
    parts = [p.strip() for p in last_line.split("\t")]
    return MountInfo(dev=parts[0], id=parts[1], point=Path(parts[2]))


def split_dsc(
    search_result: list[DSCSearchResult],
    splitter: DscSplitter = dyld_split,
) -> list[Path]:
    """
    Split DSC files into individual binaries.

    Args:
        search_result: List of DSC files to split with their target directories.
        splitter: Function to perform the split (defaults to dyld_split, injectable for testing).

    Returns:
        List of directories containing split binaries.

    Raises:
        OtaExtractError: If all split attempts fail.
    """
    split_dirs: list[Path] = []
    for result_item in search_result:
        with sentry_sdk.start_span(
            op="subprocess.dyld_split",
            name=f"Split DSC {result_item.arch}",
        ) as span:
            span.set_data("arch", str(result_item.arch))
            span.set_data("artifact", str(result_item.artifact))
            logger.info("Splitting DSC %s (%s)", result_item.artifact.name, result_item.arch)
            result = splitter(result_item.artifact, result_item.split_dir)
            if result.returncode != 0:
                logger.warning(
                    "DSC split failed for %s (%s)",
                    result_item.artifact.name,
                    result_item.arch,
                )
                span.set_status("internal_error")
            else:
                logger.info("DSC split successful for %s (%s)", result_item.artifact.name, result_item.arch)
                split_dirs.append(result_item.split_dir)

    if not split_dirs:
        artifacts = "\n".join([f"{result_item.artifact}_{result_item.arch}" for result_item in search_result])
        raise OtaExtractError(f"Split failed for all of:\n{artifacts}")

    return split_dirs


def find_dsc(input_dir: Path, version: str, build: str, output_dir: Path) -> list[DSCSearchResult]:
    # TODO: are we also interested in the DriverKit dyld_shared_cache?
    #  System/DriverKit/System/Library/dyld/
    dsc_path_prefix_options = [
        "System/Library/dyld/",
        "System/Library/Caches/com.apple.dyld/",
    ]

    counter = 1
    dsc_search_results: list[DSCSearchResult] = []
    for path_prefix in dsc_path_prefix_options:
        for arch in Arch:
            dsc_path = input_dir / (path_prefix + DYLD_SHARED_CACHE + "_" + arch)
            if os.path.isfile(dsc_path):
                split_dir = output_dir / "split_symbols" / f"{version}_{build}_{arch}"

                if any(split_dir == r.split_dir for r in dsc_search_results):
                    split_dir = split_dir.parent / f"{split_dir.name}_{counter}"
                    counter = counter + 1

                dsc_search_results.append(DSCSearchResult(arch=Arch(arch), artifact=dsc_path, split_dir=split_dir))

    if not dsc_search_results:
        raise OtaExtractError(f"Couldn't find any {DYLD_SHARED_CACHE} paths in {input_dir}")

    return dsc_search_results


def symsort(dsc_split_dir: Path, output_dir: Path, prefix: str, bundle_id: str) -> None:
    logger.info("Symsorting %s -> %s", dsc_split_dir, output_dir)

    rmdir_if_exists(output_dir)
    result = common_symsort(output_dir, prefix, bundle_id, dsc_split_dir)
    if result.returncode != 0:
        raise OtaExtractError(f"Symsorter failed with {result}")


def detach_dev(dev: str) -> None:
    subprocess.run(["hdiutil", "detach", dev], capture_output=True, check=True)
    logger.debug("Detached DMG %s", dev)


def mount_dmg(dmg: Path) -> MountInfo:
    with sentry_sdk.start_span(op="subprocess.hdiutil_mount", name=f"Mount DMG {dmg.name}"):
        result = subprocess.run(
            ["hdiutil", "mount", str(dmg)],
            capture_output=True,
            check=True,
        )
        return parse_hdiutil_mount_output(result.stdout.decode("utf-8"))


def _dir_contains_dsc(directory: Path) -> bool:
    """Check if a directory (recursively) contains any dyld_shared_cache files."""
    for _, _, files in os.walk(directory):
        for f in files:
            if f.startswith(DYLD_SHARED_CACHE):
                return True
    return False


def _classify_ota_failure(artifact: Path) -> type[Exception] | None:
    """When DSC extraction fails, classify the OTA to determine the appropriate error type.

    Runs ipsw ota info + ls once and checks for:
    - Recovery OTA (Darwin Recovery / RecoveryOSUpdate) → RecoveryOtaError
    - Delta OTA (contains image_patches/) → DeltaOtaError

    Returns the error class to raise, or None if the OTA type is unrecognized.
    """
    info_result = subprocess.run(
        ["ipsw", "ota", "info", str(artifact)],
        capture_output=True,
        text=True,
    )
    info_output = info_result.stdout + info_result.stderr
    if "Darwin Recovery" in info_output or "RecoveryOSUpdate" in info_output:
        return RecoveryOtaError

    # Check for delta indicators in the file listing:
    # - image_patches/: newer-style delta OTAs (e.g. iPad)
    # - payloadv2/patches/System/Library/Caches/com.apple.dyld/: older-style deltas (e.g. Apple TV)
    #   where the DSC itself is a binary diff
    # Note: app_patches/ alone is not sufficient: full OTAs (e.g. watchOS, visionOS) can also
    # contain app_patches/ alongside a full system image with a DSC.
    ls_result = subprocess.run(
        ["ipsw", "ota", "ls", str(artifact)],
        capture_output=True,
        text=True,
    )
    ls_output = ls_result.stdout + ls_result.stderr
    if "image_patches/" in ls_output or "payloadv2/patches/System/Library/Caches/com.apple.dyld/" in ls_output:
        return DeltaOtaError

    return None


def extract_ota(artifact: Path, output_dir: Path) -> Path | None:
    with sentry_sdk.start_span(op="subprocess.ipsw_ota_extract", name="Extract OTA DSC") as span:
        span.set_data("artifact", str(artifact))

        # First try the legacy approach: literal filename extraction (works for older OTAs)
        literal_result = subprocess.run(
            [
                "ipsw",
                "ota",
                "extract",
                str(artifact),
                DYLD_SHARED_CACHE,
                "-o",
                str(output_dir),
            ],
            capture_output=True,
        )
        pattern_result: subprocess.CompletedProcess[bytes] | None = None
        extract_dirs = list_dirs_in(output_dir)
        if not extract_dirs or not _dir_contains_dsc(extract_dirs[0]):
            # Fallback: modern payloadv2 OTAs (e.g. watchOS) store the DSC inside numbered payload
            # chunks. The literal filename lookup fails to find anything, so we use -p (pattern)
            # with -y (confirm payloadv2 search) instead.
            # Note: -d -y should work but is buggy in ipsw <=3.1.655.
            logger.info("Literal DSC extraction failed, trying payloadv2 pattern search for %s", artifact.name)
            pattern_result = subprocess.run(
                [
                    "ipsw",
                    "ota",
                    "extract",
                    str(artifact),
                    "-p",
                    DYLD_SHARED_CACHE,
                    "-y",
                    "-o",
                    str(output_dir),
                ],
                capture_output=True,
            )
            extract_dirs = list_dirs_in(output_dir)

        span.set_data("literal_extract", subprocess_result_data(literal_result))
        span.set_data("payloadv2_pattern_extract", subprocess_result_data(pattern_result))
        span.set_data("extract_dirs", _extract_dirs_data(output_dir))

        if not extract_dirs:
            span.set_status("internal_error")
            span.set_data("extract_failure_reason", f"Could not find {DYLD_SHARED_CACHE} in {artifact}")
            raise OtaExtractError(f"Could not find {DYLD_SHARED_CACHE} in {artifact}")
        elif len(extract_dirs) > 1:
            span.set_status("internal_error")
            span.set_data("extract_failure_reason", f"Found more than one image directory in {artifact}")
            raise OtaExtractError(f"Found more than one image directory in {artifact}")
        elif not _dir_contains_dsc(extract_dirs[0]):
            span.set_status("internal_error")
            span.set_data(
                "extract_failure_reason", f"OTA extraction produced no {DYLD_SHARED_CACHE} files for {artifact}"
            )
            raise OtaExtractError(f"OTA extraction produced no {DYLD_SHARED_CACHE} files for {artifact}")

        logger.info("Successfully extracted DSC from %s", artifact.name)

    return extract_dirs[0]


def extract_symbols(
    local_ota: Path,
    platform: str,
    version: str,
    build: str,
    bundle_id: str,
    work_dir: Path,
) -> list[Path]:
    """
    Extract symbols from a local OTA file. No storage interaction.

    Returns a list of directories containing symsorter output.
    """
    symbol_dirs = _try_processing_ota_as_cryptex(local_ota, platform, version, build, bundle_id, work_dir)
    if not symbol_dirs:
        logger.info("Not a cryptex, extracting OTA DSC directly")
        symbol_dirs = _process_ota_directly(local_ota, platform, version, build, bundle_id, work_dir)
    return symbol_dirs


def _try_processing_ota_as_cryptex(
    local_ota: Path, platform: str, version: str, build: str, bundle_id: str, work_dir: Path
) -> list[Path]:
    with sentry_sdk.start_span(op="ota.extract.try_cryptex", name="Try cryptex patch"):
        with tempfile.TemporaryDirectory(suffix="_cryptex_dmg") as cryptex_patch_dir:
            logger.info("Trying cryptex patch for %s", local_ota.name)
            extracted_dmgs = patch_cryptex_dmg(local_ota, Path(cryptex_patch_dir))
            if extracted_dmgs:
                logger.info("Cryptex patch successful, mounting and processing DSC for %s", local_ota.name)
                return _process_cryptex_dmg(extracted_dmgs, platform, version, build, bundle_id, work_dir)

    return []


def _process_ota_directly(
    local_ota: Path, platform: str, version: str, build: str, bundle_id: str, work_dir: Path
) -> list[Path]:
    try:
        with tempfile.TemporaryDirectory(suffix="_dsc_extract") as extract_dsc_tmp_dir:
            extracted_dsc_dir = extract_ota(local_ota, Path(extract_dsc_tmp_dir))
            logger.info("Splitting & symsorting DSC for %s", local_ota.name)

            if extracted_dsc_dir:
                return _split_and_symsort_dsc(extracted_dsc_dir, platform, version, build, bundle_id, work_dir)
    except OtaExtractError as e:
        error_cls = _classify_ota_failure(local_ota)
        if error_cls is not None:
            raise error_cls(f"{error_cls.__name__}: {local_ota}") from e
        if _should_probe_unsupported_payload(e) and _probe_unsupported_payload_format(local_ota):
            raise UnsupportedOtaPayloadError(f"Unsupported OTA payload format: {local_ota}") from e
        raise

    return []


def _split_and_symsort_dsc(
    input_dir: Path, platform: str, version: str, build: str, bundle_id: str, output_dir: Path
) -> list[Path]:
    split_dirs = split_dsc(find_dsc(input_dir, version, build, output_dir))
    return _symsort_split_results(split_dirs, platform, bundle_id, output_dir)


def _process_cryptex_dmg(
    extracted_dmgs: dict[str, Path], platform: str, version: str, build: str, bundle_id: str, output_dir: Path
) -> list[Path]:
    mount = mount_dmg(extracted_dmgs["cryptex-system-arm64e"])

    split_dirs = split_dsc(find_dsc(mount.point, version, build, output_dir))

    detach_dev(mount.dev)

    return _symsort_split_results(split_dirs, platform, bundle_id, output_dir)


def _symsort_split_results(split_dirs: list[Path], platform: str, bundle_id: str, output_dir: Path) -> list[Path]:
    symbol_output_dirs: list[Path] = []
    for split_dir in split_dirs:
        symbols_output_dir = output_dir / "symbols" / bundle_id
        symsort(
            split_dir,
            symbols_output_dir,
            platform,
            bundle_id,
        )
        symbol_output_dirs.append(symbols_output_dir)
    return symbol_output_dirs
