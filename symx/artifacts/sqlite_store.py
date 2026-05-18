"""Build compressed SQLite metadata-store candidates."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket, Client
from pydantic import BaseModel

from symx.artifacts.convert import convert_ipsw_db, convert_ota_meta
from symx.artifacts.model import ArtifactBundle, ArtifactKind
from symx.artifacts.report import ArtifactParityReport, build_parity_report
from symx.artifacts.snapshot import ArtifactSnapshotCounts, build_snapshot_db, snapshot_counts
from symx.artifacts.storage import ArtifactGcsPrefixStore, ArtifactStorageError, configure_gcs_connection_pool
from symx.gcs import parse_gcs_url
from symx.model import ArtifactProcessingState


class SqliteMetadataDbInfo(BaseModel):
    name: str
    path: str
    compressed_path: str
    artifact_count: int
    snapshot_counts: ArtifactSnapshotCounts
    raw_size_bytes: int
    compressed_size_bytes: int
    build_seconds: float
    compress_seconds: float
    integrity_check: str


class SqliteMetadataBuildResult(BaseModel):
    storage: str
    output_dir: str
    parity_ok: bool
    parity_mismatch_count: int
    dbs: list[SqliteMetadataDbInfo]


class SqliteMetadataUploadResult(BaseModel):
    storage: str
    prefix: str
    uploaded_objects: list[str]


class SqliteUpdateSimulationResult(BaseModel):
    storage: str
    object_name: str
    generation_before: int
    generation_after: int
    artifact_uid: str
    previous_state: str
    new_state: str
    compressed_size_before: int
    compressed_size_after: int
    download_seconds: float
    decompress_seconds: float
    update_seconds: float
    compress_seconds: float
    upload_seconds: float
    total_seconds: float
    integrity_check: str


class SqliteMetadataObjectStore:
    def __init__(self, bucket: Bucket, prefix: str, storage_uri: str) -> None:
        self.bucket = bucket
        self.prefix = _normalize_prefix(prefix)
        self.storage_uri = storage_uri

    @classmethod
    def from_storage_uri(cls, storage: str, prefix: str, connection_pool_size: int = 10) -> "SqliteMetadataObjectStore":
        uri = parse_gcs_url(storage)
        if uri is None or uri.hostname is None:
            raise ArtifactStorageError(f"Unsupported storage URI: {storage}")

        client: Client = Client(project=uri.username)
        configure_gcs_connection_pool(client, connection_pool_size)
        return cls(client.bucket(uri.hostname), prefix, storage)

    def object_name(self, file_name: str) -> str:
        return f"{self.prefix}/{file_name}"


def build_sqlite_metadata_from_gcs(
    storage: str, output_dir: Path, overwrite: bool = False
) -> SqliteMetadataBuildResult:
    store = ArtifactGcsPrefixStore.from_storage_uri(storage, "experiments/meta-sqlite/local-build")
    snapshot = store.load_legacy_meta()
    ipsw_bundles = convert_ipsw_db(snapshot.ipsw_db)
    ota_bundles = convert_ota_meta(snapshot.ota_meta)
    bundles = [*ipsw_bundles, *ota_bundles]
    report = build_parity_report(snapshot.ipsw_db, snapshot.ota_meta)
    return build_sqlite_metadata_files(
        storage=storage,
        output_dir=output_dir,
        bundles=bundles,
        report=report,
        overwrite=overwrite,
    )


def build_sqlite_metadata_files(
    storage: str,
    output_dir: Path,
    bundles: list[ArtifactBundle],
    report: ArtifactParityReport | None,
    overwrite: bool = False,
) -> SqliteMetadataBuildResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    dbs: list[SqliteMetadataDbInfo] = []

    dbs.append(
        _build_one_db(
            name="metadata",
            output_dir=output_dir,
            storage=storage,
            bundles=bundles,
            report=report,
            overwrite=overwrite,
        )
    )
    for kind in ArtifactKind:
        kind_bundles = [bundle for bundle in bundles if bundle.artifact.kind == kind]
        if not kind_bundles:
            continue
        dbs.append(
            _build_one_db(
                name=kind.value,
                output_dir=output_dir,
                storage=storage,
                bundles=kind_bundles,
                report=None,
                overwrite=overwrite,
            )
        )

    return SqliteMetadataBuildResult(
        storage=storage,
        output_dir=str(output_dir),
        parity_ok=report.ok if report is not None else True,
        parity_mismatch_count=len(report.mismatches) if report is not None else 0,
        dbs=dbs,
    )


def upload_sqlite_metadata_files(
    storage: str,
    prefix: str,
    input_dir: Path,
    names: Iterable[str] = ("metadata", "ipsw", "ota"),
) -> SqliteMetadataUploadResult:
    store = SqliteMetadataObjectStore.from_storage_uri(storage, prefix)
    uploaded: list[str] = []
    for name in names:
        path = input_dir / f"{name}.sqlite.gz"
        if not path.is_file():
            continue
        object_name = store.object_name(path.name)
        _upload_file_create_only(store.bucket, object_name, path)
        uploaded.append(object_name)

    if not uploaded:
        raise ArtifactStorageError(f"No sqlite.gz files found in {input_dir}")

    return SqliteMetadataUploadResult(storage=storage, prefix=store.prefix, uploaded_objects=uploaded)


def simulate_sqlite_state_update(
    storage: str,
    object_name: str,
    artifact_uid: str | None = None,
    new_state: ArtifactProcessingState = ArtifactProcessingState.IGNORED,
) -> SqliteUpdateSimulationResult:
    uri = parse_gcs_url(storage)
    if uri is None or uri.hostname is None:
        raise ArtifactStorageError(f"Unsupported storage URI: {storage}")

    client: Client = Client(project=uri.username)
    bucket = client.bucket(uri.hostname)
    blob: Blob = bucket.blob(object_name)
    if not blob.exists():
        raise ArtifactStorageError(f"Missing SQLite metadata object: {object_name}")
    blob.reload()
    generation_before = int(blob.generation or 0)
    compressed_size_before = _blob_size(blob)

    total_start = time.monotonic()
    with TemporaryDirectory(prefix="symx_sqlite_update_") as temp_dir:
        temp_path = Path(temp_dir)
        compressed_path = temp_path / "metadata.sqlite.gz"
        db_path = temp_path / "metadata.sqlite"
        updated_compressed_path = temp_path / "metadata.updated.sqlite.gz"

        download_start = time.monotonic()
        blob.download_to_filename(str(compressed_path))
        download_seconds = time.monotonic() - download_start

        decompress_start = time.monotonic()
        _gunzip_file(compressed_path, db_path)
        decompress_seconds = time.monotonic() - decompress_start

        update_start = time.monotonic()
        selected_artifact_uid, previous_state = _update_one_artifact_state(db_path, artifact_uid, new_state)
        integrity_check = _integrity_check(db_path)
        update_seconds = time.monotonic() - update_start

        compress_start = time.monotonic()
        _gzip_file(db_path, updated_compressed_path)
        compress_seconds = time.monotonic() - compress_start

        upload_start = time.monotonic()
        try:
            blob.upload_from_filename(str(updated_compressed_path), if_generation_match=generation_before)
        except PreconditionFailed as exc:
            raise ArtifactStorageError(f"Generation precondition failed for {object_name}") from exc
        upload_seconds = time.monotonic() - upload_start
        compressed_size_after = updated_compressed_path.stat().st_size

    blob.reload()
    generation_after = int(blob.generation or 0)
    total_seconds = time.monotonic() - total_start
    return SqliteUpdateSimulationResult(
        storage=storage,
        object_name=object_name,
        generation_before=generation_before,
        generation_after=generation_after,
        artifact_uid=selected_artifact_uid,
        previous_state=previous_state,
        new_state=new_state.value,
        compressed_size_before=compressed_size_before,
        compressed_size_after=compressed_size_after,
        download_seconds=round(download_seconds, 3),
        decompress_seconds=round(decompress_seconds, 3),
        update_seconds=round(update_seconds, 3),
        compress_seconds=round(compress_seconds, 3),
        upload_seconds=round(upload_seconds, 3),
        total_seconds=round(total_seconds, 3),
        integrity_check=integrity_check,
    )


def _build_one_db(
    name: str,
    output_dir: Path,
    storage: str,
    bundles: list[ArtifactBundle],
    report: ArtifactParityReport | None,
    overwrite: bool,
) -> SqliteMetadataDbInfo:
    db_path = output_dir / f"{name}.sqlite"
    compressed_path = output_dir / f"{name}.sqlite.gz"
    _check_output_path(db_path, overwrite)
    _check_output_path(compressed_path, overwrite)

    build_start = time.monotonic()
    build_snapshot_db(db_path, bundles, report=report, storage=storage, prefix=f"sqlite:{name}")
    build_seconds = time.monotonic() - build_start

    integrity_check = _integrity_check(db_path)
    counts = snapshot_counts(db_path)

    compress_start = time.monotonic()
    _gzip_file(db_path, compressed_path)
    compress_seconds = time.monotonic() - compress_start

    return SqliteMetadataDbInfo(
        name=name,
        path=str(db_path),
        compressed_path=str(compressed_path),
        artifact_count=len(bundles),
        snapshot_counts=counts,
        raw_size_bytes=db_path.stat().st_size,
        compressed_size_bytes=compressed_path.stat().st_size,
        build_seconds=round(build_seconds, 3),
        compress_seconds=round(compress_seconds, 3),
        integrity_check=integrity_check,
    )


def _blob_size(blob: Blob) -> int:
    size = cast(Any, blob).size
    return int(size) if size is not None else 0


def _normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip().strip("/")
    if not normalized:
        raise ArtifactStorageError("A non-empty SQLite metadata prefix is required")
    return normalized


def _upload_file_create_only(bucket: Bucket, object_name: str, local_file: Path) -> None:
    blob: Blob = bucket.blob(object_name)
    try:
        blob.upload_from_filename(str(local_file), if_generation_match=0)
    except PreconditionFailed as exc:
        raise ArtifactStorageError(f"Refusing to overwrite existing GCS object: {object_name}") from exc


def _gunzip_file(input_path: Path, output_path: Path) -> None:
    with gzip.open(input_path, "rb") as source, output_path.open("wb") as dest:
        shutil.copyfileobj(source, dest)


def _update_one_artifact_state(
    db_path: Path,
    artifact_uid: str | None,
    new_state: ArtifactProcessingState,
) -> tuple[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            if artifact_uid is None:
                row = conn.execute(
                    """
                    SELECT artifact_uid, processing_state
                    FROM artifacts
                    ORDER BY artifact_uid
                    LIMIT 1
                    """
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT artifact_uid, processing_state
                    FROM artifacts
                    WHERE artifact_uid = ?
                    """,
                    (artifact_uid,),
                ).fetchone()
            if row is None:
                raise ArtifactStorageError("No matching artifact row found for SQLite update simulation")

            selected_artifact_uid = str(row[0])
            previous_state = str(row[1])
            conn.execute(
                """
                UPDATE artifacts
                SET processing_state = ?
                WHERE artifact_uid = ?
                """,
                (new_state.value, selected_artifact_uid),
            )
    finally:
        conn.close()
    return selected_artifact_uid, previous_state


def _check_output_path(path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise ArtifactStorageError(f"Refusing to overwrite existing local output: {path}")
    path.unlink(missing_ok=True)


def _gzip_file(input_path: Path, output_path: Path) -> None:
    with input_path.open("rb") as source, gzip.open(output_path, "wb", compresslevel=6) as dest:
        shutil.copyfileobj(source, dest)


def _integrity_check(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    return str(row[0]) if row is not None else "missing"
