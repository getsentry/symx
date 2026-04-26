"""OTA data models, constants, protocols, exceptions, and type aliases."""

import logging
from collections.abc import Callable
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


class DSCSearchResult(BaseModel):
    model_config = {"frozen": True}

    arch: Arch
    artifact: Path
    split_dir: Path


class MountInfo(BaseModel):
    model_config = {"frozen": True}

    dev: str
    id: str
    point: Path


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

    def extract(self, local_ota: Path, ota_meta_key: str, ota_meta: OtaArtifact, work_dir: Path) -> list[Path]:
        """Run the full extract pipeline, return list of symbol directories."""
        ...


# -- Type aliases --

DscSplitter = Callable[[Path, Path], CompletedProcess[bytes]]


# -- Exceptions --


class OtaMirrorError(Exception):
    pass


class OtaExtractError(Exception):
    pass


class DeltaOtaError(Exception):
    """Raised when an OTA is identified as a delta/patch update that contains no full DSC."""

    pass


class RecoveryOtaError(Exception):
    """Raised when an OTA is a recoveryOS update (minimal boot environment, no DSC)."""

    pass


class UnsupportedOtaPayloadError(Exception):
    """Raised when an OTA references a DSC but current tooling cannot extract its payload format."""

    pass


# -- Utilities --


def parse_version_tuple(version: str) -> tuple[int, ...]:
    """Parse a version string like '26.4' or '18.2.1' into a comparable tuple."""
    try:
        return tuple(int(x) for x in version.split("."))
    except (ValueError, AttributeError):
        return (0,)
