"""Pydantic models for the normalized artifact metadata layer."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from symx.model import ArtifactProcessingState

ARTIFACT_SCHEMA_VERSION = 1


class ArtifactKind(StrEnum):
    IPSW = "ipsw"
    OTA = "ota"
    SIM = "sim"


class ArtifactSourceKind(StrEnum):
    APPLEDB = "appledb"
    APPLE_OTA_FEED = "apple_ota_feed"
    RUNNER_SIM_CACHE = "runner_sim_cache"
    XCODE_SIM = "xcode_sim"


class LegacyStore(StrEnum):
    IPSW = "ipsw"
    OTA = "ota"
    SIM = "sim"


class ArtifactLegacyRef(BaseModel):
    """Back-reference to the current v1 metadata identity."""

    store: LegacyStore
    artifact_key: str | None = None
    source_link: str | None = None
    ota_key: str | None = None
    sim_key: str | None = None


class ArtifactRecord(BaseModel):
    """Normalized storage/state metadata for a downloadable or processable file.

    This is the shared operational record. Domain-specific source interpretation belongs
    in the corresponding detail model.
    """

    schema_version: Literal[1] = ARTIFACT_SCHEMA_VERSION
    artifact_uid: str
    kind: ArtifactKind

    platform: str
    version: str
    build: str
    release_status: str | None = None
    released_at: date | None = None

    source_kind: ArtifactSourceKind
    source_url: str | None
    source_key: str
    filename: str
    size_bytes: int | None = None
    hash_algorithm: str | None = None
    hash_value: str | None = None

    mirror_path: str | None = None
    processing_state: ArtifactProcessingState

    # Existing symbolicator-facing storage identity. This must remain distinct from artifact_uid.
    symbol_store_prefix: str | None = None
    symbol_bundle_id: str | None = None

    last_run: int
    last_modified: datetime | None = None

    detail_path: str
    legacy: ArtifactLegacyRef


class IpswArtifactDetail(BaseModel):
    schema_version: Literal[1] = ARTIFACT_SCHEMA_VERSION
    artifact_uid: str
    appledb_artifact_key: str
    source_link: str
    source_index: int
    devices: list[str] = Field(default_factory=list)
    sha1: str | None = None
    sha2: str | None = None


class OtaArtifactDetail(BaseModel):
    schema_version: Literal[1] = ARTIFACT_SCHEMA_VERSION
    artifact_uid: str
    ota_key: str
    ota_id: str
    description: list[str] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)


class SimArtifactDetail(BaseModel):
    schema_version: Literal[1] = ARTIFACT_SCHEMA_VERSION
    artifact_uid: str
    sim_key: str
    runtime_identifier: str
    arch: str
    host_image: str | None = None
    xcode_version: str | None = None
    source_listing_id: str | None = None


ArtifactDetail = IpswArtifactDetail | OtaArtifactDetail | SimArtifactDetail


class ArtifactBundle(BaseModel):
    """A canonical artifact record plus exactly one domain detail record."""

    artifact: ArtifactRecord
    ipsw_detail: IpswArtifactDetail | None = None
    ota_detail: OtaArtifactDetail | None = None
    sim_detail: SimArtifactDetail | None = None

    @model_validator(mode="after")
    def validate_detail(self) -> "ArtifactBundle":
        details = [self.ipsw_detail, self.ota_detail, self.sim_detail]
        present = [detail for detail in details if detail is not None]
        if len(present) != 1:
            raise ValueError("artifact bundles must contain exactly one detail record")

        detail = present[0]
        if detail.artifact_uid != self.artifact.artifact_uid:
            raise ValueError("artifact detail UID does not match artifact record UID")

        expected_kind = {
            ArtifactKind.IPSW: self.ipsw_detail,
            ArtifactKind.OTA: self.ota_detail,
            ArtifactKind.SIM: self.sim_detail,
        }[self.artifact.kind]
        if expected_kind is None:
            raise ValueError("artifact kind does not match detail record type")

        return self

    @property
    def detail(self) -> ArtifactDetail:
        if self.ipsw_detail is not None:
            return self.ipsw_detail
        if self.ota_detail is not None:
            return self.ota_detail
        if self.sim_detail is not None:
            return self.sim_detail
        raise RuntimeError("artifact bundle has no detail record")
