"""OTA extraction pipeline: DSC materialization, splitting, and symsort."""

import logging
import plistlib
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import TypedDict

import sentry_sdk
import sentry_sdk.metrics
from pydantic import ValidationError
from sentry_sdk.tracing import Span

from symx.diagnostics import (
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
from symx.fs import rmdir_if_exists
from symx.tools import dyld_split, symsort as common_symsort
from symx.ota.model.artifact_info import OtaArtifactInfo
from symx.ota.model.ipsw_report import OtaDscReport, OtaDscReportFile
from symx.ota.model.materialization import (
    OtaDscMaterializationAttempt,
    OtaDscMaterializationError,
    OtaDscMaterializationRequest,
    OtaDscMaterializationResult,
    OtaDscNotPresent,
    OtaDscProtocolError,
    OtaDscSource,
    OtaDscUnavailable,
    OtaDscUnavailableReason,
)
from symx.ota.model import (
    DYLD_SHARED_CACHE,
    DSCSearchResult,
    DscSplitter,
    OtaClassification,
    OtaClassificationEvidence,
    OtaExtractError,
    OtaExtractionRequest,
    OtaExtractionResult,
    OtaExtractionSkipped,
    OtaExtractionSkipReason,
    OtaSymbolsExtracted,
)

logger = logging.getLogger(__name__)

PAYLOAD_FILE_NAME_RE = re.compile(r"(^|.*/)payloadv2/payload\.\d+$")
DYLD_BOM_ENTRY_RE = re.compile(
    r"^(?:\./)?(?:System/DriverKit/)?System/Library/(?:dyld|Caches/com\.apple\.dyld)/dyld_shared_cache_[^\s/]+$"
)
DYLD_LISTING_ENTRY_RE = re.compile(
    r"(?:\./)?(?:System/DriverKit/)?System/Library/(?:dyld|Caches/com\.apple\.dyld)/dyld_shared_cache_[^\s/]+"
)
MAX_IPSW_LISTING_PROBE_OUTPUT_CHARS = 1000
MAX_OTA_INFO_PLIST_BYTES = 4 * 1024 * 1024
AEA_MAGIC = b"AEA1"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_LEADING_IPSW_GLYPH_RE = re.compile(r"^\s*[•⨯]\s*")
_PREREQUISITE_BUILD_LINE_RE = re.compile(r"^PrereqBuild\s*=\s*(\S+)\s*$", re.MULTILINE)
IPSW_OTA_DSC_JSON_CONTRACT_RELEASE = "3.1.707"
MACOS_OTA_DSC_ARCHITECTURES = (Arch.ARM64E, Arch.X86_64, Arch.X86_64H)


class PayloadListingProbeResult(TypedDict):
    returncode: int
    stdout: str | None
    stderr: str | None
    dsc_entries: list[str]


def _is_clean_requested_arch_absence(report: OtaDscReport) -> bool:
    """Recognize ipsw's structured result for a requested architecture it did not find."""
    return (
        not report.files
        and bool(report.errors)
        and all(error.phase == "dsc-discovery" and error.source == "" for error in report.errors)
    )


def _ota_is_zip_archive(artifact: Path) -> bool:
    try:
        return zipfile.is_zipfile(artifact)
    except OSError:
        return False


def _ota_is_aea_archive(artifact: Path) -> bool:
    try:
        with artifact.open("rb") as f:
            return f.read(len(AEA_MAGIC)) == AEA_MAGIC
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


def _dsc_entries_from_ipsw_listing(output: str) -> list[str]:
    entries: list[str] = []
    for raw_line in _strip_ansi(output).splitlines():
        line = _normalize_ipsw_output_line(raw_line)
        match = DYLD_LISTING_ENTRY_RE.search(line)
        if match:
            entries.append(match.group(0).removeprefix("./"))
    return entries


def _probe_aea_bom_listing_for_dsc(artifact: Path) -> PayloadListingProbeResult:
    result = subprocess.run(
        ["ipsw", "ota", "ls", str(artifact), "--bom"],
        capture_output=True,
        text=True,
    )
    return {
        "returncode": result.returncode,
        "stdout": truncate_text(result.stdout, max_chars=MAX_IPSW_LISTING_PROBE_OUTPUT_CHARS),
        "stderr": truncate_text(result.stderr, max_chars=MAX_IPSW_LISTING_PROBE_OUTPUT_CHARS),
        "dsc_entries": _dsc_entries_from_ipsw_listing(result.stdout),
    }


def _probe_aea_payload_listing_for_dsc(artifact: Path) -> PayloadListingProbeResult:
    result = subprocess.run(
        ["ipsw", "ota", "ls", str(artifact), "--payload", "--pattern", DYLD_SHARED_CACHE],
        capture_output=True,
        text=True,
    )
    return {
        "returncode": result.returncode,
        "stdout": truncate_text(result.stdout, max_chars=MAX_IPSW_LISTING_PROBE_OUTPUT_CHARS),
        "stderr": truncate_text(result.stderr, max_chars=MAX_IPSW_LISTING_PROBE_OUTPUT_CHARS),
        "dsc_entries": _dsc_entries_from_ipsw_listing(result.stdout),
    }


def _probe_payload_dsc_inventory(artifact: Path) -> None:
    """Record DSC inventory after a payload-extract failure without materializing files."""
    with sentry_sdk.start_span(op="ota.extract.payload_probe", name="Inspect OTA payload DSC inventory") as span:
        span.set_data("artifact", str(artifact))

        try:
            if not _ota_is_zip_archive(artifact):
                span.set_data("is_zip_archive", False)
                is_aea_archive = _ota_is_aea_archive(artifact)
                span.set_data("is_aea_archive", is_aea_archive)
                if not is_aea_archive:
                    span.set_data("dsc_referenced", False)
                    return

                payload_listing = _probe_aea_payload_listing_for_dsc(artifact)
                payload_dsc_entries = payload_listing["dsc_entries"]
                span.set_data(
                    "aea_payload_listing_probe",
                    {
                        "returncode": payload_listing["returncode"],
                        "stdout": payload_listing["stdout"],
                        "stderr": payload_listing["stderr"],
                        "dsc_entry_count": len(payload_dsc_entries),
                        "dsc_entries_sample": payload_dsc_entries[:10],
                    },
                )
                if payload_listing["returncode"] == 0 and payload_dsc_entries:
                    span.set_data("dsc_referenced", True)
                    return

                # `ipsw ota ls --bom` can describe post-state files for partial OTAs, so BOM matches are
                # evidence that the OTA references a DSC, not proof that every DSC byte is present.
                bom_listing = _probe_aea_bom_listing_for_dsc(artifact)
                bom_dsc_entries = bom_listing["dsc_entries"]
                span.set_data(
                    "aea_bom_listing_probe",
                    {
                        "returncode": bom_listing["returncode"],
                        "stdout": bom_listing["stdout"],
                        "stderr": bom_listing["stderr"],
                        "dsc_entry_count": len(bom_dsc_entries),
                        "dsc_entries_sample": bom_dsc_entries[:10],
                    },
                )
                dsc_referenced = bom_listing["returncode"] == 0 and bool(bom_dsc_entries)
                span.set_data("dsc_referenced", dsc_referenced)
                return

            span.set_data("is_zip_archive", True)
            span.set_data("is_aea_archive", False)
            bom_matches = _read_post_bom_dsc_matches(artifact)
            span.set_data("post_bom_dsc_match_count", len(bom_matches))
            span.set_data("post_bom_dsc_matches", bom_matches[:10])
            if not bom_matches:
                span.set_data("dsc_referenced", False)
                return

            payload_names = _payload_entry_names(artifact)
            span.set_data("payload_member_count", len(payload_names))
            span.set_data("payload_members_sample", payload_names[:10])
            span.set_data("dsc_referenced", True)
        except (FileNotFoundError, OSError, ValueError, zipfile.BadZipFile) as exc:
            span.set_data("payload_probe_error", str(exc))


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _normalize_ipsw_output_line(line: str) -> str:
    return _LEADING_IPSW_GLYPH_RE.sub("", line).strip()


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


def symsort(dsc_split_dirs: list[Path], output_dir: Path, prefix: str, bundle_id: str) -> None:
    logger.info("Symsorting %d DSC split directories -> %s", len(dsc_split_dirs), output_dir)

    rmdir_if_exists(output_dir)
    result = common_symsort(output_dir, prefix, bundle_id, dsc_split_dirs)
    if result.returncode != 0:
        raise OtaExtractError(f"Symsorter failed with {result}")


def _unavailable_classification_evidence(
    platform: str,
    *,
    metadata_source: str = "unavailable",
) -> OtaClassificationEvidence:
    return OtaClassificationEvidence(
        platform=platform,
        info_succeeded=False,
        prerequisite_build=None,
        metadata_source=metadata_source,
    )


def _parse_ota_info_plist(data: bytes) -> OtaArtifactInfo:
    if len(data) > MAX_OTA_INFO_PLIST_BYTES:
        raise ValueError(f"root Info.plist is too large: {len(data)} bytes")
    return OtaArtifactInfo.model_validate(plistlib.loads(data))


def _read_zip_ota_classification_evidence(request: OtaExtractionRequest) -> OtaClassificationEvidence:
    try:
        with zipfile.ZipFile(request.local_ota) as archive:
            info_entry = archive.getinfo("Info.plist")
            if info_entry.file_size > MAX_OTA_INFO_PLIST_BYTES:
                raise ValueError(f"root Info.plist is too large: {info_entry.file_size} bytes")
            info = _parse_ota_info_plist(archive.read(info_entry))
    except (KeyError, OSError, ValueError, plistlib.InvalidFileException, ValidationError, zipfile.BadZipFile) as error:
        logger.warning("Could not read trusted OTA metadata from %s: %s", request.local_ota, error)
        return _unavailable_classification_evidence(request.platform)

    return OtaClassificationEvidence(
        platform=request.platform,
        info_succeeded=True,
        prerequisite_build=info.prerequisite_build,
        metadata_source="zip-info-plist",
    )


def _extract_aea_ota_info(request: OtaExtractionRequest) -> OtaClassificationEvidence | None:
    """Try to reconstruct a root Info.plist from an AEA without materializing symbols."""
    with tempfile.TemporaryDirectory(suffix="_ota_info") as output_dir:
        command = [
            "ipsw",
            "--no-color",
            "ota",
            "extract",
            str(request.local_ota),
            "--pattern",
            r"^Info\.plist$",
            "--confirm",
            "--output",
            output_dir,
        ]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True)
        except OSError as error:
            logger.warning("Could not invoke AEA OTA metadata extractor for %s: %s", request.local_ota, error)
            return None

        parsed: list[OtaArtifactInfo] = []
        output_root = Path(output_dir).resolve()
        for candidate in Path(output_dir).rglob("Info.plist"):
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(output_root):
                    continue
                mode = candidate.stat(follow_symlinks=False).st_mode
                if not stat.S_ISREG(mode) or candidate.stat().st_size > MAX_OTA_INFO_PLIST_BYTES:
                    continue
                parsed.append(_parse_ota_info_plist(candidate.read_bytes()))
            except (OSError, ValueError, plistlib.InvalidFileException, ValidationError):
                continue

        prerequisite_builds = {info.prerequisite_build for info in parsed}
        if len(prerequisite_builds) == 1:
            prerequisite_build = prerequisite_builds.pop()
            logger.info(
                "Read AEA OTA classification metadata from extracted Info.plist for %s (extract exit %d)",
                request.local_ota,
                result.returncode,
            )
            return OtaClassificationEvidence(
                platform=request.platform,
                info_succeeded=True,
                prerequisite_build=prerequisite_build,
                metadata_source="aea-extracted-info-plist",
            )
        if parsed:
            logger.warning(
                "AEA OTA metadata extraction returned conflicting Info.plist files for %s", request.local_ota
            )
            return _unavailable_classification_evidence(
                request.platform,
                metadata_source="aea-extracted-info-plist-conflict",
            )

        logger.info(
            "AEA OTA metadata extraction did not return a usable root Info.plist for %s (exit %d): %s",
            request.local_ota,
            result.returncode,
            truncate_text(result.stderr) or "<empty stderr>",
        )
        return None


def _read_aea_info_text_fallback(request: OtaExtractionRequest) -> OtaClassificationEvidence:
    """Temporary fallback until structured ipsw AEA metadata includes PrerequisiteBuild."""
    command = ["ipsw", "--no-color", "ota", "info", str(request.local_ota)]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as error:
        logger.warning("Could not invoke AEA OTA metadata fallback for %s: %s", request.local_ota, error)
        return _unavailable_classification_evidence(request.platform)
    if result.returncode != 0:
        logger.warning(
            "Could not inspect AEA OTA metadata for %s (exit %d): %s",
            request.local_ota,
            result.returncode,
            truncate_text(result.stderr) or "<empty stderr>",
        )
        return _unavailable_classification_evidence(request.platform)

    prerequisite_builds = set(_PREREQUISITE_BUILD_LINE_RE.findall(result.stdout + "\n" + result.stderr))
    if len(prerequisite_builds) > 1:
        logger.warning("AEA OTA metadata fallback returned conflicting prerequisite builds for %s", request.local_ota)
        return _unavailable_classification_evidence(request.platform)

    prerequisite_build = next(iter(prerequisite_builds), None)
    logger.info("Read AEA OTA classification metadata from ipsw info text fallback for %s", request.local_ota)
    return OtaClassificationEvidence(
        platform=request.platform,
        info_succeeded=True,
        prerequisite_build=prerequisite_build,
        metadata_source="ipsw-info-text-fallback",
    )


def _collect_ota_classification_evidence(request: OtaExtractionRequest) -> OtaClassificationEvidence:
    """Read artifact metadata only after materialization finds no usable DSC."""
    if request.platform == "recovery":
        return OtaClassificationEvidence(
            platform=request.platform,
            info_succeeded=True,
            prerequisite_build=None,
            metadata_source="request-platform",
        )
    if zipfile.is_zipfile(request.local_ota):
        return _read_zip_ota_classification_evidence(request)
    if _ota_is_aea_archive(request.local_ota):
        return _extract_aea_ota_info(request) or _read_aea_info_text_fallback(request)
    return _unavailable_classification_evidence(request.platform)


def _classify_ota_evidence(evidence: OtaClassificationEvidence) -> OtaClassification:
    """Apply pure policy to trusted request context and typed artifact metadata."""
    if evidence.platform == "recovery":
        return OtaClassification.RECOVERY
    if evidence.info_succeeded and evidence.prerequisite_build:
        return OtaClassification.DELTA
    return OtaClassification.UNKNOWN


def _classify_ota(request: OtaExtractionRequest) -> OtaClassification:
    return _classify_ota_evidence(_collect_ota_classification_evidence(request))


def _parse_ota_dsc_report(
    request: OtaDscMaterializationRequest,
    result: subprocess.CompletedProcess[bytes],
) -> OtaDscReport:
    try:
        return OtaDscReport.model_validate_json(result.stdout or b"")
    except ValidationError as error:
        raise OtaDscProtocolError(
            f"ipsw did not return a valid schema-1 JSON report for {request.local_ota} "
            f"(exit code {result.returncode}): {error.errors(include_url=False)[0]['msg']}"
        ) from error


def _validate_report_process_contract(
    request: OtaDscMaterializationRequest,
    result: subprocess.CompletedProcess[bytes],
    report: OtaDscReport,
) -> None:
    if report.complete and report.errors:
        raise OtaDscProtocolError(f"ipsw returned complete=true with structured errors for {request.local_ota}")
    if not report.complete and not report.errors:
        raise OtaDscProtocolError(f"ipsw returned complete=false without structured errors for {request.local_ota}")
    if report.complete != (result.returncode == 0):
        raise OtaDscProtocolError(
            f"ipsw report completeness disagrees with exit code {result.returncode} for {request.local_ota}"
        )
    if report.complete and not report.files:
        raise OtaDscProtocolError(f"ipsw returned a complete report with no DSC files for {request.local_ota}")


def _reported_dsc_arch(path: PurePosixPath) -> str:
    name = path.name
    if name.startswith("aot_shared_cache."):
        return "aot"
    if not name.startswith(f"{DYLD_SHARED_CACHE}_"):
        return ""
    return name.removeprefix(f"{DYLD_SHARED_CACHE}_").split(".", 1)[0]


def _validate_reported_files(
    request: OtaDscMaterializationRequest,
    report: OtaDscReport,
) -> list[tuple[OtaDscReportFile, PurePosixPath, Path]]:
    output_root = request.output_root.resolve(strict=False)
    seen_paths: set[Path] = set()
    validated: list[tuple[OtaDscReportFile, PurePosixPath, Path]] = []

    for entry in report.files:
        relative_path = PurePosixPath(entry.path)
        if not relative_path.parts or relative_path.is_absolute() or ".." in relative_path.parts or "\\" in entry.path:
            raise OtaDscProtocolError(
                f"ipsw report path must be a relative path beneath {request.output_root}: {entry.path!r}"
            )

        artifact = request.output_root.joinpath(*relative_path.parts)
        try:
            resolved_artifact = artifact.resolve(strict=True)
        except OSError as error:
            raise OtaDscProtocolError(f"ipsw reported DSC is not an existing regular file: {entry.path!r}") from error

        if not resolved_artifact.is_relative_to(output_root):
            raise OtaDscProtocolError(
                f"ipsw report path must be a relative path beneath {request.output_root}: {entry.path!r}"
            )
        if resolved_artifact in seen_paths:
            raise OtaDscProtocolError(f"ipsw report contains duplicate path: {entry.path!r}")
        seen_paths.add(resolved_artifact)

        # Deliberately reject a reported leaf symlink: ipsw must materialize an actual regular file.
        try:
            mode = artifact.stat(follow_symlinks=False).st_mode
        except OSError as error:
            raise OtaDscProtocolError(f"ipsw reported DSC is not an existing regular file: {entry.path!r}") from error
        if not stat.S_ISREG(mode):
            raise OtaDscProtocolError(f"ipsw reported DSC is not a regular file: {entry.path!r}")

        reported_arch = _reported_dsc_arch(relative_path)
        if reported_arch != entry.arch:
            raise OtaDscProtocolError(f"ipsw report architecture {entry.arch!r} does not match path {entry.path!r}")

        validated.append((entry, relative_path, artifact))

    return validated


def _path_contains_sequence(parts: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    width = len(sequence)
    return any(parts[index : index + width] == sequence for index in range(len(parts) - width + 1))


def _supported_dsc_source(
    entry: OtaDscReportFile,
    relative_path: PurePosixPath,
    artifact: Path,
) -> OtaDscSource | None:
    try:
        arch = Arch(entry.arch)
    except ValueError:
        return None

    if relative_path.name != f"{DYLD_SHARED_CACHE}_{arch}":
        return None

    parts = relative_path.parts
    if _path_contains_sequence(parts, ("System", "DriverKit", "System", "Library")):
        return None
    if _path_contains_sequence(parts, ("System", "x86Support", "System", "Library")):
        return None

    parent_parts = relative_path.parent.parts
    supported_parent = parent_parts[-3:] == ("System", "Library", "dyld") or parent_parts[-4:] == (
        "System",
        "Library",
        "Caches",
        "com.apple.dyld",
    )
    if not supported_parent:
        return None

    return OtaDscSource(arch=arch, artifact=artifact)


def _source_kind(source: str) -> str:
    if source in {"ota-asset", "payloadv2"}:
        return source
    if source.startswith("cryptex-"):
        return "cryptex"
    return "unknown"


def _set_materialization_report_data(span: Span, report: OtaDscReport) -> None:
    span.set_data(
        "report",
        {
            "schema_version": report.schema_version,
            "complete": report.complete,
            "reported_dsc_count": len(report.files),
            "architectures": sorted({entry.arch for entry in report.files}),
            "source_kinds": sorted({_source_kind(entry.source) for entry in report.files}),
            "errors": [
                {
                    "phase": error.phase,
                    "source": error.source,
                    "message": truncate_text(error.message, max_chars=500),
                }
                for error in report.errors
            ],
        },
    )


def _record_materialization_unavailable(
    span: Span,
    request: OtaDscMaterializationRequest,
    unavailable: OtaDscUnavailable,
) -> OtaDscUnavailable:
    span.set_data("unavailable_reason", str(unavailable.reason))
    sentry_sdk.metrics.count(
        "ota.extract.materialization.unavailable",
        1,
        attributes={"platform": request.platform, "reason": str(unavailable.reason)},
    )
    if unavailable.reason == OtaDscUnavailableReason.INCOMPLETE:
        span.set_status("internal_error")
        span.set_data("failure_reason", unavailable.message)
        sentry_sdk.metrics.count("ota.extract.materialization.failed", 1, attributes={"platform": request.platform})
        sentry_sdk.metrics.count("ota.extract.materialization.incomplete", 1, attributes={"platform": request.platform})
    return unavailable


def extract_ota(request: OtaDscMaterializationRequest) -> OtaDscMaterializationAttempt:
    """Materialize and validate one requested architecture, or the unfiltered non-macOS set."""
    with sentry_sdk.start_span(op="ota.extract.materialize_dsc", name="Materialize OTA DSCs") as span:
        span.set_data("artifact", str(request.local_ota))
        span.set_data("output_root", str(request.output_root))
        span.set_data("platform", request.platform)
        span.set_data("version", request.version)
        span.set_data("build", request.build)
        span.set_data("source_identity", request.bundle_id)
        span.set_data("requested_arch", str(request.requested_arch) if request.requested_arch is not None else None)
        span.set_data("ipsw_contract_release", IPSW_OTA_DSC_JSON_CONTRACT_RELEASE)

        command = [
            "ipsw",
            "--no-color",
            "ota",
            "extract",
            str(request.local_ota),
            "--dyld",
        ]
        if request.requested_arch is not None:
            command.extend(["--dyld-arch", str(request.requested_arch)])
        command.extend(
            [
                "--json",
                "--output",
                str(request.output_root),
            ]
        )
        logger.info("Materializing OTA DSCs: %s", format_command(command))
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True)
        except OSError as error:
            span.set_status("internal_error")
            span.set_data("failure_reason", str(error))
            sentry_sdk.metrics.count("ota.extract.materialization.failed", 1, attributes={"platform": request.platform})
            raise OtaDscProtocolError(f"Could not invoke OTA DSC materializer: {format_command(command)}") from error

        span.set_data(
            "subprocess",
            {
                "command": format_command(command),
                **subprocess_result_data(result),
            },
        )
        stderr = truncate_text(result.stderr)
        if stderr is not None:
            logger.info("ipsw OTA DSC diagnostics for %s:\n%s", request.local_ota.name, stderr)

        try:
            report = _parse_ota_dsc_report(request, result)
            _set_materialization_report_data(span, report)
            _validate_report_process_contract(request, result, report)
            validated_files = _validate_reported_files(request, report)

            if request.requested_arch is not None and _is_clean_requested_arch_absence(report):
                sentry_sdk.metrics.count(
                    "ota.extract.materialization.not_present",
                    1,
                    attributes={"platform": request.platform, "arch": str(request.requested_arch)},
                )
                logger.info(
                    "OTA %s does not contain a %s DSC",
                    request.local_ota.name,
                    request.requested_arch,
                )
                return OtaDscNotPresent(arch=request.requested_arch, report=report)

            if not report.complete:
                phases = ", ".join(sorted({error.phase for error in report.errors}))
                return _record_materialization_unavailable(
                    span,
                    request,
                    OtaDscUnavailable(
                        reason=OtaDscUnavailableReason.INCOMPLETE,
                        report=report,
                        message=(
                            f"OTA DSC materialization was incomplete for {request.local_ota} "
                            f"with exit code {result.returncode}; phases={phases}"
                        ),
                    ),
                )

            if request.requested_arch is not None:
                mismatched_arches = sorted(
                    {entry.arch for entry, _, _ in validated_files if entry.arch != request.requested_arch}
                )
                if mismatched_arches:
                    raise OtaDscProtocolError(
                        f"ipsw reported architecture(s) {', '.join(mismatched_arches)} for "
                        f"requested architecture {request.requested_arch}"
                    )

            dscs = tuple(
                dsc
                for entry, relative_path, artifact in validated_files
                if (dsc := _supported_dsc_source(entry, relative_path, artifact)) is not None
            )
            span.set_data(
                "report_validation",
                {
                    "validated_dsc_count": len(validated_files),
                    "supported_primary_dsc_count": len(dscs),
                },
            )

            if not dscs:
                architectures = ", ".join(sorted({entry.arch for entry in report.files}))
                return _record_materialization_unavailable(
                    span,
                    request,
                    OtaDscUnavailable(
                        reason=OtaDscUnavailableReason.NO_SUPPORTED_PRIMARY,
                        report=report,
                        message=(
                            f"OTA DSC materialization produced no supported primary {DYLD_SHARED_CACHE} files "
                            f"for {request.local_ota}; reported architectures={architectures}"
                        ),
                    ),
                )
            if request.requested_arch is not None and (len(dscs) != 1 or dscs[0].arch != request.requested_arch):
                raise OtaDscProtocolError(
                    f"ipsw reported {len(dscs)} supported primary DSCs for requested architecture "
                    f"{request.requested_arch}; expected exactly one"
                )
        except OtaExtractError as error:
            span.set_status("internal_error")
            span.set_data("failure_reason", str(error))
            sentry_sdk.metrics.count("ota.extract.materialization.failed", 1, attributes={"platform": request.platform})
            raise

        sentry_sdk.metrics.count("ota.extract.materialization.succeeded", 1, attributes={"platform": request.platform})
        sentry_sdk.metrics.distribution(
            "ota.extract.materialized_dscs", len(validated_files), attributes={"platform": request.platform}
        )
        logger.info("Materialized %d supported DSC(s) from %s", len(dscs), request.local_ota.name)
        return OtaDscMaterializationResult(dscs=dscs)


def extract_symbols(request: OtaExtractionRequest) -> OtaExtractionResult:
    """Extract symbols or return an expected artifact disposition without storage side effects."""
    return _process_ota(request)


def _resolve_unavailable_materialization(
    request: OtaExtractionRequest,
    unavailable: OtaDscUnavailable,
) -> OtaExtractionSkipped:
    if unavailable.exhausted_sources_without_primary:
        classification = _classify_ota(request)
        if classification == OtaClassification.DELTA:
            return OtaExtractionSkipped(reason=OtaExtractionSkipReason.DELTA)
        if classification == OtaClassification.RECOVERY:
            return OtaExtractionSkipped(reason=OtaExtractionSkipReason.RECOVERY)

    if unavailable.has_payload_extraction_failure:
        _probe_payload_dsc_inventory(request.local_ota)

    raise OtaDscMaterializationError(unavailable)


def _process_ota(request: OtaExtractionRequest) -> OtaExtractionResult:
    if request.platform == "macos":
        return _process_macos_ota(request)

    with tempfile.TemporaryDirectory(suffix="_dsc_extract") as extract_dsc_tmp_dir:
        materialization_request = OtaDscMaterializationRequest.from_extraction_request(
            request,
            output_root=Path(extract_dsc_tmp_dir),
        )
        match extract_ota(materialization_request):
            case OtaDscNotPresent():
                raise OtaDscProtocolError("unfiltered OTA materialization returned an architecture absence")
            case OtaDscUnavailable() as unavailable:
                return _resolve_unavailable_materialization(request, unavailable)
            case OtaDscMaterializationResult(dscs=dscs):
                logger.info("Splitting & symsorting DSC for %s", request.local_ota.name)
                search_results = _dsc_search_results(dscs, request)
                split_dirs = split_dsc(search_results)
                symbol_dirs = _symsort_split_results(
                    split_dirs,
                    request.platform,
                    request.bundle_id,
                    request.work_dir,
                )
                return OtaSymbolsExtracted(symbol_dirs=tuple(symbol_dirs))


def _process_macos_ota(request: OtaExtractionRequest) -> OtaExtractionResult:
    archives: list[tuple[Path, Path]] = []
    split_dirs_to_clean: list[Path] = []
    absent: list[OtaDscNotPresent] = []
    primary_error = False

    try:
        for arch in MACOS_OTA_DSC_ARCHITECTURES:
            with sentry_sdk.start_span(
                op="ota.extract.dsc_arch",
                name=f"Materialize+split OTA DSC {arch}",
            ) as span:
                span.set_data("arch", str(arch))
                with tempfile.TemporaryDirectory(suffix=f"_{arch}_dsc_extract") as materialization_dir:
                    materialization_request = OtaDscMaterializationRequest.from_extraction_request(
                        request,
                        output_root=Path(materialization_dir),
                        requested_arch=arch,
                    )
                    match extract_ota(materialization_request):
                        case OtaDscNotPresent() as not_present:
                            absent.append(not_present)
                            continue
                        case OtaDscUnavailable() as unavailable:
                            return _resolve_unavailable_materialization(request, unavailable)
                        case OtaDscMaterializationResult(dscs=dscs):
                            search_results = _dsc_search_results(dscs, request)
                            split_dirs_to_clean.extend(result.split_dir for result in search_results)
                            split_dirs = split_dsc(search_results)
                            if len(split_dirs) != 1:
                                raise OtaExtractError(
                                    f"Expected one split directory for macOS OTA architecture {arch}, "
                                    f"got {len(split_dirs)}"
                                )

                            split_dir = split_dirs[0]
                            archive_path = split_dir.parent / f"{split_dir.name}.tar.zst"
                            archives.append((archive_path, split_dir))
                            try:
                                compressed = compress_directory(split_dir)
                            except (DirectoryArchiveError, OSError) as error:
                                raise OtaExtractError(f"Could not compress macOS OTA {arch} split: {error}") from error
                            if compressed != archive_path:
                                raise OtaExtractError(
                                    f"Unexpected macOS OTA split archive path {compressed}; expected {archive_path}"
                                )

        if not archives:
            if not absent:
                raise OtaDscProtocolError("macOS OTA architecture search produced no result")
            unavailable = OtaDscUnavailable(
                reason=OtaDscUnavailableReason.NO_SUPPORTED_PRIMARY,
                report=absent[-1].report,
                message=(
                    "OTA DSC materialization found none of the requested macOS architectures: "
                    f"{', '.join(str(arch) for arch in MACOS_OTA_DSC_ARCHITECTURES)}"
                ),
            )
            return _resolve_unavailable_materialization(request, unavailable)

        if request.owns_local_ota and request.local_ota.exists():
            logger.info("Deleting workflow-owned OTA after materialization: %s", request.local_ota)
            try:
                request.local_ota.unlink()
            except OSError as error:
                raise OtaExtractError(f"Could not remove workflow-owned OTA {request.local_ota}: {error}") from error

        restored_split_dirs: list[Path] = []
        for archive_path, split_dir in archives:
            try:
                decompress_archive(archive_path, split_dir)
                archive_path.unlink()
            except (DirectoryArchiveError, OSError) as error:
                raise OtaExtractError(f"Could not restore macOS OTA split {archive_path}: {error}") from error
            restored_split_dirs.append(split_dir)

        symbol_dirs = _symsort_split_results(
            restored_split_dirs,
            request.platform,
            request.bundle_id,
            request.work_dir,
        )
        return OtaSymbolsExtracted(symbol_dirs=tuple(symbol_dirs))
    except BaseException:
        primary_error = True
        raise
    finally:
        cleanup_errors: list[OSError] = []
        for archive_path, _ in archives:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError as error:
                cleanup_errors.append(error)
        for split_dir in split_dirs_to_clean:
            try:
                rmdir_if_exists(split_dir)
            except OSError as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            cleanup_error = OtaExtractError(
                "Could not clean macOS OTA split intermediates: " + "; ".join(str(error) for error in cleanup_errors)
            )
            if primary_error:
                sentry_sdk.capture_exception(cleanup_error)
                logger.error("%s", cleanup_error)
            else:
                raise cleanup_error


def _dsc_search_results(
    dscs: tuple[OtaDscSource, ...],
    request: OtaExtractionRequest,
) -> list[DSCSearchResult]:
    arch_counts: dict[Arch, int] = {}
    search_results: list[DSCSearchResult] = []
    for dsc in dscs:
        occurrence = arch_counts.get(dsc.arch, 0)
        arch_counts[dsc.arch] = occurrence + 1
        split_dir = request.work_dir / "split_symbols" / f"{request.version}_{request.build}_{dsc.arch}"
        if occurrence:
            split_dir = split_dir.parent / f"{split_dir.name}_{occurrence}"
        search_results.append(DSCSearchResult(arch=dsc.arch, artifact=dsc.artifact, split_dir=split_dir))
    return search_results


def _symsort_split_results(split_dirs: list[Path], platform: str, bundle_id: str, output_dir: Path) -> list[Path]:
    symbols_output_dir = output_dir / "symbols" / bundle_id
    symsort(
        split_dirs,
        symbols_output_dir,
        platform,
        bundle_id,
    )
    return [symbols_output_dir]
