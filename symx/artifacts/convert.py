"""Converters from the current IPSW/OTA metadata models to normalized artifacts."""

from __future__ import annotations

from symx.artifacts.ids import detail_object_path, ipsw_artifact_uid, ota_artifact_uid
from symx.artifacts.model import (
    ArtifactBundle,
    ArtifactKind,
    ArtifactLegacyRef,
    ArtifactRecord,
    IpswArtifactDetail,
    MetadataSource,
    LegacyStore,
    OtaArtifactDetail,
)
from symx.ipsw.extract import generate_bundle_id as generate_ipsw_bundle_id
from symx.ipsw.extract import map_platform_to_prefix as map_ipsw_platform_to_symbol_prefix
from symx.ipsw.model import IpswArtifact, IpswArtifactDb, IpswSource
from symx.ota.model import OtaArtifact, OtaMetaData


def convert_ipsw_db(ipsw_db: IpswArtifactDb) -> list[ArtifactBundle]:
    bundles: list[ArtifactBundle] = []
    for artifact in ipsw_db.artifacts.values():
        bundles.extend(convert_ipsw_artifact(artifact))
    return bundles


def convert_ipsw_artifact(artifact: IpswArtifact) -> list[ArtifactBundle]:
    bundles: list[ArtifactBundle] = []
    for source_index, source in enumerate(artifact.sources):
        bundles.append(convert_ipsw_source(artifact, source, source_index))
    return bundles


def convert_ipsw_source(artifact: IpswArtifact, source: IpswSource, source_index: int) -> ArtifactBundle:
    source_link = str(source.link)
    artifact_uid = ipsw_artifact_uid(artifact.key, source_link)
    hash_algorithm, hash_value = _ipsw_hash(source)
    detail_path = detail_object_path(ArtifactKind.IPSW, artifact_uid)

    record = ArtifactRecord(
        artifact_uid=artifact_uid,
        kind=ArtifactKind.IPSW,
        platform=artifact.platform.value,
        version=artifact.version,
        build=artifact.build,
        release_status=artifact.release_status.value,
        released_at=artifact.released,
        metadata_source=MetadataSource.APPLEDB,
        source_url=source_link,
        source_key=f"{artifact.key}:{source_index}",
        filename=source.file_name,
        size_bytes=source.size,
        hash_algorithm=hash_algorithm,
        hash_value=hash_value,
        mirror_path=source.mirror_path,
        processing_state=source.processing_state,
        symbol_store_prefix=map_ipsw_platform_to_symbol_prefix(artifact.platform),
        symbol_bundle_id=generate_ipsw_bundle_id(source.file_name),
        last_run=source.last_run,
        last_modified=source.last_modified,
        detail_path=detail_path,
        legacy=ArtifactLegacyRef(
            store=LegacyStore.IPSW,
            artifact_key=artifact.key,
            source_link=source_link,
        ),
    )
    hashes = source.hashes
    detail = IpswArtifactDetail(
        artifact_uid=artifact_uid,
        appledb_artifact_key=artifact.key,
        source_link=source_link,
        source_index=source_index,
        devices=sorted(source.devices),
        sha1=hashes.sha1 if hashes is not None else None,
        sha2=hashes.sha2 if hashes is not None else None,
    )
    return ArtifactBundle(artifact=record, ipsw_detail=detail)


def convert_ota_meta(ota_meta: OtaMetaData) -> list[ArtifactBundle]:
    return [convert_ota_artifact(ota_key, ota) for ota_key, ota in ota_meta.items()]


def convert_ota_artifact(ota_key: str, ota: OtaArtifact) -> ArtifactBundle:
    artifact_uid = ota_artifact_uid(ota_key)
    detail_path = detail_object_path(ArtifactKind.OTA, artifact_uid)
    filename = ota_filename(ota)

    record = ArtifactRecord(
        artifact_uid=artifact_uid,
        kind=ArtifactKind.OTA,
        platform=ota.platform,
        version=ota.version,
        build=ota.build,
        release_status=None,
        released_at=None,
        metadata_source=MetadataSource.APPLE_OTA_FEED,
        source_url=ota.url,
        source_key=ota_key,
        filename=filename,
        size_bytes=None,
        hash_algorithm=ota.hash_algorithm,
        hash_value=ota.hash,
        mirror_path=ota.download_path,
        processing_state=ota.processing_state,
        symbol_store_prefix=ota.platform,
        symbol_bundle_id=f"ota_{ota_key}",
        last_run=ota.last_run,
        last_modified=None,
        detail_path=detail_path,
        legacy=ArtifactLegacyRef(
            store=LegacyStore.OTA,
            ota_key=ota_key,
        ),
    )
    detail = OtaArtifactDetail(
        artifact_uid=artifact_uid,
        ota_key=ota_key,
        ota_id=ota.id,
        description=sorted(ota.description),
        devices=sorted(ota.devices),
    )
    return ArtifactBundle(artifact=record, ota_detail=detail)


def ota_filename(ota: OtaArtifact) -> str:
    return f"{ota.platform}_{ota.version}_{ota.build}_{ota.id}.zip"


def _ipsw_hash(source: IpswSource) -> tuple[str | None, str | None]:
    hashes = source.hashes
    if hashes is None:
        return None, None
    if hashes.sha1 is not None:
        return "sha1", hashes.sha1
    if hashes.sha2 is not None:
        return "sha2-256", hashes.sha2
    return None, None
