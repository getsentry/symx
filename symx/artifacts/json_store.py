"""Legacy JSON metadata-store experiment helpers."""

from __future__ import annotations

import json
import time
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket, Client
from pydantic import BaseModel

from symx.admin.meta_json import parse_ota_meta_json
from symx.artifacts.storage import ArtifactStorageError, configure_gcs_connection_pool
from symx.gcs import parse_gcs_url
from symx.ipsw.model import ARTIFACTS_META_JSON as IPSW_ARTIFACTS_META_JSON
from symx.ipsw.model import IpswArtifactDb
from symx.model import ArtifactProcessingState
from symx.ota.model import ARTIFACTS_META_JSON as OTA_ARTIFACTS_META_JSON


class JsonMetadataKind(StrEnum):
    IPSW = "ipsw"
    OTA = "ota"


class JsonMetadataUploadResult(BaseModel):
    storage: str
    prefix: str
    uploaded_objects: list[str]


class JsonUpdateSimulationResult(BaseModel):
    storage: str
    object_name: str
    kind: JsonMetadataKind
    generation_before: int
    generation_after: int
    selected_key: str
    previous_state: str
    new_state: str
    size_before: int
    size_after: int
    download_seconds: float
    update_seconds: float
    upload_seconds: float
    total_seconds: float


class JsonMetadataObjectStore:
    def __init__(self, bucket: Bucket, prefix: str, storage_uri: str) -> None:
        self.bucket = bucket
        self.prefix = _normalize_prefix(prefix)
        self.storage_uri = storage_uri

    @classmethod
    def from_storage_uri(cls, storage: str, prefix: str, connection_pool_size: int = 10) -> "JsonMetadataObjectStore":
        uri = parse_gcs_url(storage)
        if uri is None or uri.hostname is None:
            raise ArtifactStorageError(f"Unsupported storage URI: {storage}")

        client: Client = Client(project=uri.username)
        configure_gcs_connection_pool(client, connection_pool_size)
        return cls(client.bucket(uri.hostname), prefix, storage)

    def object_name(self, file_name: str) -> str:
        return f"{self.prefix}/{file_name}"


def upload_legacy_json_copies(storage: str, prefix: str) -> JsonMetadataUploadResult:
    store = JsonMetadataObjectStore.from_storage_uri(storage, prefix)
    uploaded = [
        _copy_object_create_only(store.bucket, IPSW_ARTIFACTS_META_JSON, store.object_name(IPSW_ARTIFACTS_META_JSON)),
        _copy_object_create_only(store.bucket, OTA_ARTIFACTS_META_JSON, store.object_name(OTA_ARTIFACTS_META_JSON)),
    ]
    return JsonMetadataUploadResult(storage=storage, prefix=store.prefix, uploaded_objects=uploaded)


def simulate_json_state_update(
    storage: str,
    object_name: str,
    kind: JsonMetadataKind,
    new_state: ArtifactProcessingState = ArtifactProcessingState.IGNORED,
) -> JsonUpdateSimulationResult:
    uri = parse_gcs_url(storage)
    if uri is None or uri.hostname is None:
        raise ArtifactStorageError(f"Unsupported storage URI: {storage}")

    client: Client = Client(project=uri.username)
    bucket = client.bucket(uri.hostname)
    blob: Blob = bucket.blob(object_name)
    if not blob.exists():
        raise ArtifactStorageError(f"Missing JSON metadata object: {object_name}")
    blob.reload()
    generation_before = int(blob.generation or 0)
    size_before = _blob_size(blob)

    total_start = time.monotonic()
    with TemporaryDirectory(prefix="symx_json_update_") as temp_dir:
        json_path = Path(temp_dir) / "metadata.json"

        download_start = time.monotonic()
        blob.download_to_filename(str(json_path))
        download_seconds = time.monotonic() - download_start

        update_start = time.monotonic()
        selected_key, previous_state, payload = _updated_json_payload(json_path.read_text(), kind, new_state)
        json_path.write_text(payload)
        update_seconds = time.monotonic() - update_start

        upload_start = time.monotonic()
        try:
            blob.upload_from_filename(str(json_path), if_generation_match=generation_before)
        except PreconditionFailed as exc:
            raise ArtifactStorageError(f"Generation precondition failed for {object_name}") from exc
        upload_seconds = time.monotonic() - upload_start
        size_after = json_path.stat().st_size

    blob.reload()
    generation_after = int(blob.generation or 0)
    total_seconds = time.monotonic() - total_start
    return JsonUpdateSimulationResult(
        storage=storage,
        object_name=object_name,
        kind=kind,
        generation_before=generation_before,
        generation_after=generation_after,
        selected_key=selected_key,
        previous_state=previous_state,
        new_state=new_state.value,
        size_before=size_before,
        size_after=size_after,
        download_seconds=round(download_seconds, 3),
        update_seconds=round(update_seconds, 3),
        upload_seconds=round(upload_seconds, 3),
        total_seconds=round(total_seconds, 3),
    )


def _updated_json_payload(
    payload: str,
    kind: JsonMetadataKind,
    new_state: ArtifactProcessingState,
) -> tuple[str, str, str]:
    match kind:
        case JsonMetadataKind.IPSW:
            db = IpswArtifactDb.model_validate_json(payload)
            artifact_key = sorted(db.artifacts)[0]
            artifact = db.artifacts[artifact_key]
            source = artifact.sources[0]
            previous_state = source.processing_state.value
            source.processing_state = new_state
            return f"{artifact_key}:{source.file_name}", previous_state, db.model_dump_json()
        case JsonMetadataKind.OTA:
            meta = parse_ota_meta_json(payload, ArtifactStorageError)
            ota_key = sorted(meta)[0]
            artifact = meta[ota_key]
            previous_state = artifact.processing_state.value
            artifact.processing_state = new_state
            serializable = {key: value.model_dump(mode="json") for key, value in meta.items()}
            return ota_key, previous_state, json.dumps(serializable)


def _copy_object_create_only(bucket: Bucket, source_name: str, dest_name: str) -> str:
    source_blob = bucket.blob(source_name)
    if not source_blob.exists():
        raise ArtifactStorageError(f"Missing source object: {source_name}")

    dest_blob = bucket.blob(dest_name)
    with TemporaryDirectory(prefix="symx_json_copy_") as temp_dir:
        local_path = Path(temp_dir) / source_name
        source_blob.download_to_filename(str(local_path))
        try:
            dest_blob.upload_from_filename(str(local_path), if_generation_match=0)
        except PreconditionFailed as exc:
            raise ArtifactStorageError(f"Refusing to overwrite existing GCS object: {dest_name}") from exc
    return dest_name


def _normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip().strip("/")
    if not normalized:
        raise ArtifactStorageError("A non-empty JSON metadata prefix is required")
    return normalized


def _blob_size(blob: Blob) -> int:
    size = cast(Any, blob).size
    return int(size) if size is not None else 0
