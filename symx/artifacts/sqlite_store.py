"""Build compressed SQLite metadata-store candidates."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel

from symx.artifacts.convert import convert_ipsw_db, convert_ota_meta
from symx.artifacts.model import ArtifactBundle, ArtifactKind
from symx.artifacts.report import ArtifactParityReport, build_parity_report
from symx.artifacts.snapshot import ArtifactSnapshotCounts, build_snapshot_db, snapshot_counts
from symx.artifacts.storage import ArtifactGcsPrefixStore, ArtifactStorageError


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
