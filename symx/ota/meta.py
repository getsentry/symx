"""OTA meta-data parsing, retrieval from Apple, and merge logic."""

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlparse

import sentry_sdk
import sentry_sdk.metrics
from pydantic import BaseModel, ConfigDict, Field

from symx.diagnostics import decode_subprocess_output, format_command, truncate_text
from symx.model import ArtifactProcessingState
from symx.ota.model import (
    PLATFORMS,
    OtaArtifact,
    OtaDelivery,
    OtaMetaData,
)

logger = logging.getLogger(__name__)

type OtaArtifactIdentity = tuple[str, str, str, str, str, str]
type IndexedOtaIdentity = tuple[bool, OtaArtifactIdentity]


class AppleOtaChannel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    release_type: str | None = None
    documentation_id: str | None = None


class AppleOtaPrerequisite(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    build: str | None = None
    version: str | None = None


class AppleOtaProvenance(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    asset_type: str | None = None


class AppleOtaMetaItem(BaseModel):
    """Strict subset of one schema-1 item emitted by ``ipsw download ota --json``."""

    model_config = ConfigDict(extra="ignore", strict=True)

    url: str
    build: str
    version: str
    sha1: str = Field(min_length=1)
    delivery: OtaDelivery
    supported_devices: list[str] = Field(default_factory=list)
    supported_models: list[str] = Field(default_factory=list)
    channel: AppleOtaChannel | None = None
    prerequisite: AppleOtaPrerequisite | None = None
    provenance: AppleOtaProvenance | None = None


class AppleOtaMetaHeader(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    schema_version: int


class AppleOtaMetaEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    schema_version: Literal[1]
    otas: list[AppleOtaMetaItem]


def parse_download_meta_output(
    platform: str,
    result: subprocess.CompletedProcess[bytes],
    meta_data: OtaMetaData,
    beta: bool,
) -> None:
    if result.returncode != 0:
        ipsw_stderr = decode_subprocess_output(result.stderr)
        # We regularly get 403 errors on the apple endpoint. These seem to be intermittent
        # availability issues and do not warrant error notification noise.
        if "api returned status: 403 Forbidden" not in ipsw_stderr:
            logger.error(
                "Download OTA meta failed for %s%s (exit %s): %s",
                platform,
                " (beta)" if beta else "",
                result.returncode,
                truncate_text(ipsw_stderr) or "<empty stderr>",
            )
    else:
        raw_platform_meta: object = json.loads(result.stdout)
        header = AppleOtaMetaHeader.model_validate(raw_platform_meta)
        if header.schema_version != 1:
            raise ValueError(f"Unsupported ipsw OTA metadata schema version: {header.schema_version}")
        platform_meta = AppleOtaMetaEnvelope.model_validate(raw_platform_meta)
        retrieved_at = datetime.now(UTC)

        for meta_item in platform_meta.otas:
            url = meta_item.url
            sentry_sdk.set_tag("artifact.url", url)
            artifact_id = PurePosixPath(urlparse(url).path).stem
            # Artifact IDs have historically been either SHA-1 (40 hex digits) or SHA-256 (64 hex digits).
            if len(artifact_id) not in (40, 64):
                logger.error("Parsing download meta: unexpected url-format")

            channel = meta_item.channel
            prerequisite = meta_item.prerequisite
            provenance = meta_item.provenance
            description = (
                [channel.documentation_id] if channel is not None and channel.documentation_id is not None else []
            )

            if beta:
                # Betas can have the same artifact ID as later releases, often with the same contents.
                key = artifact_id + "_beta"
            else:
                key = artifact_id

            artifact = OtaArtifact(
                id=artifact_id,
                build=meta_item.build,
                description=description,
                version=meta_item.version,
                platform=platform,
                url=url,
                devices=meta_item.supported_devices,
                supported_models=meta_item.supported_models,
                download_path=None,
                hash=meta_item.sha1,
                hash_algorithm="SHA-1",
                release_type=channel.release_type if channel is not None else None,
                asset_type=provenance.asset_type if provenance is not None else None,
                delivery=meta_item.delivery,
                prerequisite_build=prerequisite.build if prerequisite is not None else None,
                prerequisite_version=prerequisite.version if prerequisite is not None else None,
                last_modified=retrieved_at,
            )
            if key in meta_data:
                key = generate_duplicate_key_from(meta_data, key)
                artifact.processing_state = ArtifactProcessingState.INDEXED_DUPLICATE
            meta_data[key] = artifact


def _download_meta_job(job: tuple[str, bool]) -> OtaMetaData:
    platform, beta = job
    label = f"{platform} (beta)" if beta else platform

    with sentry_sdk.start_span(op="subprocess.ipsw_download_meta", name=f"Fetch OTA meta for {label}") as span:
        span.set_data("platform", platform)
        span.set_data("beta", beta)
        logger.info("Downloading OTA meta for %s%s", platform, " (beta)" if beta else "")
        cmd = [
            "ipsw",
            "download",
            "ota",
            "--platform",
            platform,
            "--json",
        ]
        if beta:
            cmd.append("--beta")

        span.set_data("command", format_command(cmd))
        result = subprocess.run(cmd, capture_output=True)
        span.set_data("returncode", result.returncode)
        span.set_data("stderr", truncate_text(result.stderr))
        meta: OtaMetaData = {}
        parse_download_meta_output(platform, result, meta, beta)
        return meta


def retrieve_current_meta() -> OtaMetaData:
    meta: OtaMetaData = {}
    jobs = [(platform, beta) for platform in PLATFORMS for beta in (False, True)]

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        for job_meta in executor.map(_download_meta_job, jobs):
            meta.update(job_meta)

    sentry_sdk.metrics.distribution("ota.meta_sync.total_artifacts", len(meta))
    return meta


def generate_duplicate_key_from(ours: OtaMetaData, their_key: str) -> str:
    duplicate_num = 1
    key_candidate = f"{their_key}_duplicate_{duplicate_num}"

    while key_candidate in ours:
        duplicate_num += 1
        key_candidate = f"{their_key}_duplicate_{duplicate_num}"

    return key_candidate


def merge_meta_data(ours: OtaMetaData, theirs: OtaMetaData) -> None:
    """Merge fresh Apple metadata without replacing persisted workflow state."""

    def identity(item: OtaArtifact) -> OtaArtifactIdentity:
        return (
            item.build,
            item.version,
            item.platform,
            item.url,
            item.hash,
            item.hash_algorithm,
        )

    def is_beta_key(key: str, item: OtaArtifact) -> bool:
        return key.startswith(f"{item.id}_beta")

    def base_key(key: str, item: OtaArtifact) -> str:
        return f"{item.id}_beta" if is_beta_key(key, item) else item.id

    def merge_lists(existing: list[str], fresh: list[str]) -> list[str]:
        return list(dict.fromkeys(existing + fresh))

    def hydrate(existing: OtaArtifact, fresh: OtaArtifact) -> None:
        existing.description = merge_lists(existing.description, fresh.description)
        existing.devices = merge_lists(existing.devices, fresh.devices)
        existing.supported_models = merge_lists(existing.supported_models, fresh.supported_models)
        for field in (
            "release_type",
            "asset_type",
            "delivery",
            "prerequisite_build",
            "prerequisite_version",
        ):
            if (fresh_value := getattr(fresh, field)) is not None:
                setattr(existing, field, fresh_value)

    def is_content_alias(existing: OtaArtifact, fresh: OtaArtifact) -> bool:
        if (
            existing.platform != fresh.platform
            or existing.hash != fresh.hash
            or existing.hash_algorithm != fresh.hash_algorithm
        ):
            return False
        return existing.url == fresh.url or (existing.build == fresh.build and existing.version == fresh.version)

    identities: dict[IndexedOtaIdentity, str] = {}
    for key, item in ours.items():
        identities.setdefault((is_beta_key(key, item), identity(item)), key)

    for their_key, their_item in theirs.items():
        indexed_identity = (is_beta_key(their_key, their_item), identity(their_item))
        if matching_key := identities.get(indexed_identity):
            hydrate(ours[matching_key], their_item)
            continue

        if their_key in ours:
            our_item = ours[their_key]
            if not is_content_alias(our_item, their_item):
                raise RuntimeError(f"Matching keys with different value:\n\tlocal: {our_item}\n\tapple: {their_item}")

            duplicate_key = generate_duplicate_key_from(ours, base_key(their_key, their_item))
            their_item.processing_state = ArtifactProcessingState.INDEXED_DUPLICATE
            ours[duplicate_key] = their_item
            identities[indexed_identity] = duplicate_key
            continue

        ours[their_key] = their_item
        identities[indexed_identity] = their_key

        # Identify and mark beta <-> normal release duplicates.
        for our_key, our_item in ours.items():
            if our_key == their_key:
                continue
            if (
                their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
                and their_item.platform == our_item.platform
                and their_item.version == our_item.version
                and their_item.build != our_item.build
            ):
                their_item.processing_state = ArtifactProcessingState.INDEXED_DUPLICATE
                break
