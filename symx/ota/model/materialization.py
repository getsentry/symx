"""Trusted runtime values for the OTA DSC materialization adapter.

Application code constructs requests from validated workflow state; the adapter
constructs sources and results after validating the external ``ipsw`` report.
These values are operation context, not persistence or wire schemas. Non-success
outcomes retain the parsed report needed for diagnostics and classification.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from symx.model import Arch
from symx.ota.model.ipsw_report import OtaDscReport
from symx.ota.model import OtaExtractError, OtaExtractionRequest


@dataclass(frozen=True)
class OtaDscMaterializationRequest:
    """Complete context for one external OTA DSC materialization operation."""

    local_ota: Path
    output_root: Path
    platform: str
    version: str
    build: str
    bundle_id: str
    requested_arch: Arch | None = None

    @classmethod
    def from_extraction_request(
        cls,
        request: OtaExtractionRequest,
        *,
        output_root: Path,
        requested_arch: Arch | None = None,
    ) -> "OtaDscMaterializationRequest":
        return cls(
            local_ota=request.local_ota,
            output_root=output_root,
            platform=request.platform,
            version=request.version,
            build=request.build,
            bundle_id=request.bundle_id,
            requested_arch=requested_arch,
        )


@dataclass(frozen=True)
class OtaDscSource:
    """A validated primary DSC supported by the existing Symx split policy."""

    arch: Arch
    artifact: Path


@dataclass(frozen=True)
class OtaDscMaterializationResult:
    """Materialization produced one or more supported primary DSCs."""

    dscs: tuple[OtaDscSource, ...]


@dataclass(frozen=True)
class OtaDscNotPresent:
    """A requested architecture was not found after a complete source search."""

    arch: Arch
    report: OtaDscReport


class OtaDscUnavailableReason(StrEnum):
    """Why a valid ipsw report did not produce usable materialization input."""

    INCOMPLETE = "incomplete"
    NO_SUPPORTED_PRIMARY = "no_supported_primary"


@dataclass(frozen=True)
class OtaDscUnavailable:
    """A valid report that cannot be accepted as complete Symx input."""

    reason: OtaDscUnavailableReason
    report: OtaDscReport
    message: str

    @property
    def exhausted_sources_without_primary(self) -> bool:
        """Whether all attempted sources yielded no supported primary DSC."""
        if self.reason == OtaDscUnavailableReason.NO_SUPPORTED_PRIMARY:
            return True
        return not self.report.files and any(error.phase == "dsc-discovery" for error in self.report.errors)

    @property
    def has_payload_extraction_failure(self) -> bool:
        return any(error.phase == "payload-extract" for error in self.report.errors)


OtaDscMaterializationAttempt = OtaDscMaterializationResult | OtaDscNotPresent | OtaDscUnavailable


class OtaDscProtocolError(OtaExtractError):
    """Raised when ipsw does not honor the required schema-1 subprocess contract."""


class OtaDscMaterializationError(OtaExtractError):
    """Raised after an unavailable materialization outcome cannot be classified."""

    def __init__(self, unavailable: OtaDscUnavailable) -> None:
        super().__init__(unavailable.message)
        self.unavailable = unavailable
        self.report = unavailable.report
