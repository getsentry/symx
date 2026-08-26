"""Trusted runtime values for the OTA DSC materialization adapter.

Application code constructs requests from validated workflow state; the adapter
constructs sources and results after validating the external ``ipsw`` report.
These values are operation context, not persistence or wire schemas. Only
failures retain the parsed report needed for diagnostics and classification.
"""

from dataclasses import dataclass
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
    dscs: tuple[OtaDscSource, ...]


@dataclass(frozen=True)
class OtaDscNotPresent:
    """A requested architecture was not found after a complete source search."""

    arch: Arch
    report: OtaDscReport


OtaDscMaterializationAttempt = OtaDscMaterializationResult | OtaDscNotPresent


class OtaDscProtocolError(OtaExtractError):
    """Raised when ipsw does not honor the required schema-1 subprocess contract."""


class OtaDscMaterializationError(OtaExtractError):
    """Raised for a valid report that cannot be accepted as complete Symx input."""

    def __init__(self, message: str, report: OtaDscReport) -> None:
        super().__init__(message)
        self.report = report


class OtaDscUnsupportedPrimaryError(OtaDscMaterializationError):
    """Raised when materialization produces files but no supported primary DSC."""
