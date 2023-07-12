import logging
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field
from pydantic import HttpUrl

from symx._common import ArtifactProcessingState

logger = logging.getLogger(__name__)


ARTIFACTS_META_JSON = "ipsw_meta.json"


class IpswReleaseStatus(StrEnum):
    RELEASE = "rel"
    RELEASE_CANDIDATE = "rc"
    BETA = "beta"


class IpswPlatform(StrEnum):
    AUDIOOS = "audioOS"
    BRIDGEOS = "bridgeOS"
    IOS = "iOS"
    IPADOS = "iPadOS"
    IPODOS = "iPodOS"
    MACOS = "macOS"
    TVOS = "tvOS"
    WATCHOS = "watchOS"


class IpswArtifactHashes(BaseModel):
    sha1: str | None = None
    sha2: str | None = Field(None, validation_alias="sha2-256")


class IpswSource(BaseModel):
    devices: list[str]
    link: HttpUrl
    hashes: IpswArtifactHashes | None = None
    size: int | None = None


class IpswArtifact(BaseModel):
    platform: IpswPlatform
    version: str
    build: str
    released: date | None = None
    release_status: IpswReleaseStatus
    sources: list[IpswSource]
    processing_state: ArtifactProcessingState = ArtifactProcessingState.INDEXED

    @computed_field  # type: ignore[misc]
    @property
    def key(self) -> str:
        return f"{self.platform}_{self.version}_{self.build}"


class IpswArtifactDb(BaseModel):
    version: int = 0
    artifacts: dict[str, IpswArtifact] = {}

    def contains(self, key: str) -> bool:
        return key in self.artifacts
