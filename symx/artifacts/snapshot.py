"""Read-optimized SQLite snapshots for normalized artifact metadata."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel

from symx.artifacts.model import ArtifactBundle, ArtifactKind
from symx.artifacts.report import ArtifactParityReport


class ArtifactSnapshotCounts(BaseModel):
    artifacts: int
    ipsw_details: int
    ota_details: int
    sim_details: int


class _SnapshotMetadata(BaseModel):
    generated_at: str
    artifact_count: int
    parity_ok: bool
    parity_mismatch_count: int


def build_snapshot_db(
    db_path: Path,
    bundles: list[ArtifactBundle],
    report: ArtifactParityReport | None,
    storage: str,
    prefix: str,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    snapshot_metadata = _snapshot_metadata(bundles, report)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _init_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO snapshot_info (
                    id,
                    generated_at,
                    storage,
                    prefix,
                    artifact_count,
                    parity_ok,
                    parity_mismatch_count
                )
                VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_metadata.generated_at,
                    storage,
                    prefix,
                    snapshot_metadata.artifact_count,
                    int(snapshot_metadata.parity_ok),
                    snapshot_metadata.parity_mismatch_count,
                ),
            )
            for bundle in bundles:
                artifact = bundle.artifact
                conn.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_uid,
                        kind,
                        metadata_source,
                        platform,
                        version,
                        build,
                        release_status,
                        released_at,
                        source_url,
                        source_key,
                        filename,
                        size_bytes,
                        hash_algorithm,
                        hash_value,
                        mirror_path,
                        processing_state,
                        symbol_store_prefix,
                        symbol_bundle_id,
                        last_run,
                        last_modified,
                        detail_path,
                        legacy_store,
                        legacy_artifact_key,
                        legacy_source_link,
                        legacy_ota_key,
                        legacy_sim_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_uid,
                        artifact.kind.value,
                        artifact.metadata_source.value,
                        artifact.platform,
                        artifact.version,
                        artifact.build,
                        artifact.release_status,
                        _date_value(artifact.released_at),
                        artifact.source_url,
                        artifact.source_key,
                        artifact.filename,
                        artifact.size_bytes,
                        artifact.hash_algorithm,
                        artifact.hash_value,
                        artifact.mirror_path,
                        artifact.processing_state.value,
                        artifact.symbol_store_prefix,
                        artifact.symbol_bundle_id,
                        artifact.last_run,
                        _datetime_value(artifact.last_modified),
                        artifact.detail_path,
                        artifact.legacy.store.value,
                        artifact.legacy.artifact_key,
                        artifact.legacy.source_link,
                        artifact.legacy.ota_key,
                        artifact.legacy.sim_key,
                    ),
                )
                if artifact.kind == ArtifactKind.IPSW and bundle.ipsw_detail is not None:
                    detail = bundle.ipsw_detail
                    conn.execute(
                        """
                        INSERT INTO ipsw_details (
                            artifact_uid,
                            appledb_artifact_key,
                            source_link,
                            source_index,
                            devices_json,
                            sha1,
                            sha2
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            detail.artifact_uid,
                            detail.appledb_artifact_key,
                            detail.source_link,
                            detail.source_index,
                            _json_list(detail.devices),
                            detail.sha1,
                            detail.sha2,
                        ),
                    )
                elif artifact.kind == ArtifactKind.OTA and bundle.ota_detail is not None:
                    detail = bundle.ota_detail
                    conn.execute(
                        """
                        INSERT INTO ota_details (
                            artifact_uid,
                            ota_key,
                            ota_id,
                            description_json,
                            devices_json
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            detail.artifact_uid,
                            detail.ota_key,
                            detail.ota_id,
                            _json_list(detail.description),
                            _json_list(detail.devices),
                        ),
                    )
                elif artifact.kind == ArtifactKind.SIM and bundle.sim_detail is not None:
                    detail = bundle.sim_detail
                    conn.execute(
                        """
                        INSERT INTO sim_details (
                            artifact_uid,
                            sim_key,
                            runtime_identifier,
                            arch,
                            host_image,
                            xcode_version,
                            source_listing_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            detail.artifact_uid,
                            detail.sim_key,
                            detail.runtime_identifier,
                            detail.arch,
                            detail.host_image,
                            detail.xcode_version,
                            detail.source_listing_id,
                        ),
                    )
    finally:
        conn.close()


def snapshot_counts(db_path: Path) -> ArtifactSnapshotCounts:
    conn = sqlite3.connect(db_path)
    try:
        return ArtifactSnapshotCounts(
            artifacts=_count_rows(conn, "artifacts"),
            ipsw_details=_count_rows(conn, "ipsw_details"),
            ota_details=_count_rows(conn, "ota_details"),
            sim_details=_count_rows(conn, "sim_details"),
        )
    finally:
        conn.close()


def _snapshot_metadata(bundles: list[ArtifactBundle], report: ArtifactParityReport | None) -> _SnapshotMetadata:
    if report is not None:
        return _SnapshotMetadata(
            generated_at=report.generated_at,
            artifact_count=report.total_artifacts,
            parity_ok=report.ok,
            parity_mismatch_count=len(report.mismatches),
        )

    return _SnapshotMetadata(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        artifact_count=len(bundles),
        parity_ok=True,
        parity_mismatch_count=0,
    )


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE snapshot_info (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            generated_at TEXT NOT NULL,
            storage TEXT NOT NULL,
            prefix TEXT NOT NULL,
            artifact_count INTEGER NOT NULL,
            parity_ok INTEGER NOT NULL,
            parity_mismatch_count INTEGER NOT NULL
        );

        CREATE TABLE artifacts (
            artifact_uid TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            metadata_source TEXT NOT NULL,
            platform TEXT NOT NULL,
            version TEXT NOT NULL,
            build TEXT NOT NULL,
            release_status TEXT,
            released_at TEXT,
            source_url TEXT,
            source_key TEXT NOT NULL,
            filename TEXT NOT NULL,
            size_bytes INTEGER,
            hash_algorithm TEXT,
            hash_value TEXT,
            mirror_path TEXT,
            processing_state TEXT NOT NULL,
            symbol_store_prefix TEXT,
            symbol_bundle_id TEXT,
            last_run INTEGER NOT NULL,
            last_modified TEXT,
            detail_path TEXT NOT NULL,
            legacy_store TEXT NOT NULL,
            legacy_artifact_key TEXT,
            legacy_source_link TEXT,
            legacy_ota_key TEXT,
            legacy_sim_key TEXT
        );

        CREATE TABLE ipsw_details (
            artifact_uid TEXT PRIMARY KEY REFERENCES artifacts(artifact_uid) ON DELETE CASCADE,
            appledb_artifact_key TEXT NOT NULL,
            source_link TEXT NOT NULL,
            source_index INTEGER NOT NULL,
            devices_json TEXT NOT NULL,
            sha1 TEXT,
            sha2 TEXT
        );

        CREATE TABLE ota_details (
            artifact_uid TEXT PRIMARY KEY REFERENCES artifacts(artifact_uid) ON DELETE CASCADE,
            ota_key TEXT NOT NULL,
            ota_id TEXT NOT NULL,
            description_json TEXT NOT NULL,
            devices_json TEXT NOT NULL
        );

        CREATE TABLE sim_details (
            artifact_uid TEXT PRIMARY KEY REFERENCES artifacts(artifact_uid) ON DELETE CASCADE,
            sim_key TEXT NOT NULL,
            runtime_identifier TEXT NOT NULL,
            arch TEXT NOT NULL,
            host_image TEXT,
            xcode_version TEXT,
            source_listing_id TEXT
        );

        CREATE INDEX artifacts_kind_state_idx ON artifacts(kind, processing_state);
        CREATE INDEX artifacts_platform_version_idx ON artifacts(platform, version, build);
        CREATE INDEX artifacts_legacy_idx ON artifacts(legacy_store, legacy_artifact_key, legacy_ota_key);
        CREATE INDEX artifacts_symbol_bundle_idx ON artifacts(symbol_store_prefix, symbol_bundle_id);
        """
    )


def _count_rows(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0])


def _date_value(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _json_list(values: list[str]) -> str:
    return "[" + ",".join(_json_string(value) for value in values) + "]"


def _json_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
