"""Core OTA domain models, interfaces, and shared errors.

Persisted ``OtaArtifact`` data uses Pydantic validation; trusted runtime values
use dataclasses. The external ipsw schema and its adapter values live in
the ``ipsw_report`` and ``materialization`` submodules respectively.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol

from pydantic import BaseModel

from symx.model import (
    Arch,
    ArtifactProcessingState,
    github_run_id,
)

logger = logging.getLogger(__name__)

PLATFORMS = [
    "ios",
    "watchos",
    "tvos",
    "audioos",
    "accessory",
    "macos",
    "recovery",
    "visionos",
]

ARTIFACTS_META_JSON = "ota_image_meta.json"

DYLD_SHARED_CACHE = "dyld_shared_cache"


# -- Data models --


class OtaArtifact(BaseModel):
    build: str
    description: list[str]
    version: str
    platform: str
    id: str
    url: str
    download_path: str | None
    devices: list[str]
    hash: str
    hash_algorithm: str

    # currently the run_id of the GHA Workflow so we can look it up
    # TODO: add a `last_modified` field like IPSW has and migrate old meta-data offline by
    #  hydrating it from the existing JSON plus `last_run`/fetch context where available.
    last_run: int = github_run_id()
    processing_state: ArtifactProcessingState = ArtifactProcessingState.INDEXED

    def is_indexed(self) -> bool:
        return self.processing_state == ArtifactProcessingState.INDEXED

    def is_mirrored(self) -> bool:
        return self.processing_state == ArtifactProcessingState.MIRRORED

    def update_last_run(self) -> None:
        self.last_run = github_run_id()


OtaMetaData = dict[str, OtaArtifact]


@dataclass(frozen=True)
class OtaExtractionRequest:
    local_ota: Path
    work_dir: Path
    platform: str
    version: str
    build: str
    bundle_id: str
    owns_local_ota: bool = False

    @classmethod
    def from_artifact(
        cls,
        *,
        local_ota: Path,
        work_dir: Path,
        meta_key: str,
        artifact: OtaArtifact,
    ) -> "OtaExtractionRequest":
        return cls(
            local_ota=local_ota,
            work_dir=work_dir,
            platform=artifact.platform,
            version=artifact.version,
            build=artifact.build,
            bundle_id=f"ota_{meta_key}",
            owns_local_ota=True,
        )


@dataclass(frozen=True)
class OtaSymbolsExtracted:
    """Successful extraction output ready for symbol upload."""

    symbol_dirs: tuple[Path, ...]


class OtaExtractionSkipReason(StrEnum):
    """Expected artifact dispositions that do not produce symbols."""

    DELTA = "delta"
    RECOVERY = "recovery"
    UNSUPPORTED_PAYLOAD = "unsupported_payload"


@dataclass(frozen=True)
class OtaExtractionSkipped:
    """An expected terminal extraction disposition."""

    reason: OtaExtractionSkipReason


OtaExtractionResult = OtaSymbolsExtracted | OtaExtractionSkipped


class OtaClassification(StrEnum):
    """Best-effort diagnosis after materialization finds no usable DSC."""

    DELTA = "delta"
    RECOVERY = "recovery"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OtaClassificationEvidence:
    """Trusted request context and typed artifact metadata used by classification policy."""

    platform: str
    info_succeeded: bool
    prerequisite_build: str | None
    metadata_source: str


@dataclass(frozen=True)
class DSCSearchResult:
    arch: Arch
    artifact: Path
    split_dir: Path


# -- Protocols --


class OtaStorage(Protocol):
    def save_meta(self, theirs: OtaMetaData) -> OtaMetaData: ...

    def save_ota(self, ota_meta_key: str, ota_meta: OtaArtifact, ota_file: Path) -> None: ...

    def load_meta(self) -> OtaMetaData | None: ...

    def load_ota(self, ota: OtaArtifact, download_dir: Path) -> Path | None: ...

    def name(self) -> str: ...

    def update_meta_item(self, ota_meta_key: str, ota_meta: OtaArtifact) -> OtaMetaData: ...

    def upload_symbols(self, input_dir: Path, ota_meta_key: str, ota_meta: OtaArtifact, bundle_id: str) -> None: ...


class OtaMetaRetriever(Protocol):
    def retrieve(self) -> OtaMetaData:
        """Fetch current OTA meta-data from Apple."""
        ...


class OtaDownloader(Protocol):
    def download(self, ota_meta: OtaArtifact, download_dir: Path) -> Path:
        """Download an OTA from Apple, verify hash, return local path. Raises on failure."""
        ...


class OtaSymbolExtractor(Protocol):
    def validate_deps(self) -> None: ...

    def extract(self, request: OtaExtractionRequest) -> OtaExtractionResult:
        """Run extraction and return either symbols or an expected skip disposition."""
        ...


# -- Type aliases --

DscSplitter = Callable[[Path, Path], CompletedProcess[bytes]]


# -- Exceptions --


class OtaMirrorError(Exception):
    pass


class OtaExtractError(Exception):
    pass


# -- Utilities --


def parse_version_tuple(version: str) -> tuple[int, ...]:
    """Parse a version string like '26.4' or '18.2.1' into a comparable tuple."""
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError(f"Unexpected OTA version format: {version!r}")
    return tuple(int(part) for part in parts)
