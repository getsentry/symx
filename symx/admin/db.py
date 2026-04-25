from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from symx.ipsw.model import IpswArtifactDb
from symx.model import ArtifactProcessingState
from symx.ota.model import OtaArtifact

# Keep the default filter limited to states actively emitted by current automation.
DEFAULT_FAILURE_STATES: Final[tuple[ArtifactProcessingState, ...]] = (
    ArtifactProcessingState.MIRRORING_FAILED,
    ArtifactProcessingState.MIRROR_CORRUPT,
    ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
    ArtifactProcessingState.INDEXED_INVALID,
)

MANIFEST_FILE_NAME: Final[str] = "manifest.json"
SNAPSHOT_DB_FILE_NAME: Final[str] = "snapshot.db"
SNAPSHOTS_DIR_NAME: Final[str] = "snapshots"


@dataclass(frozen=True)
class SnapshotManifest:
    active_snapshot_id: str | None = None


@dataclass(frozen=True)
class SnapshotPaths:
    snapshot_id: str
    root: Path
    db_path: Path
    ipsw_meta_path: Path
    ota_meta_path: Path
    ipsw_blob_path: Path
    ota_blob_path: Path


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    created_at: str
    workflow_run_id: int | None
    workflow_run_url: str | None
    ipsw_generation: int
    ota_generation: int


@dataclass(frozen=True)
class IpswSourceRow:
    last_modified: str | None
    processing_state: ArtifactProcessingState
    platform: str
    version: str
    build: str
    artifact_key: str
    file_name: str
    link: str
    sha1: str | None
    last_run: int
    mirror_path: str | None


@dataclass(frozen=True)
class OtaArtifactRow:
    last_run: int
    processing_state: ArtifactProcessingState
    platform: str
    version: str
    build: str
    ota_key: str
    artifact_id: str
    url: str
    hash: str
    hash_algorithm: str
    download_path: str | None


IpswFailureRow = IpswSourceRow
OtaFailureRow = OtaArtifactRow


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "symx" / "admin"


def manifest_path(cache_dir: Path) -> Path:
    return cache_dir / MANIFEST_FILE_NAME


def snapshots_dir(cache_dir: Path) -> Path:
    return cache_dir / SNAPSHOTS_DIR_NAME


def make_snapshot_id(ipsw_generation: int, ota_generation: int) -> str:
    return f"ipsw-{ipsw_generation}__ota-{ota_generation}"


def snapshot_paths(cache_dir: Path, snapshot_id: str) -> SnapshotPaths:
    root = snapshots_dir(cache_dir) / snapshot_id
    return SnapshotPaths(
        snapshot_id=snapshot_id,
        root=root,
        db_path=root / SNAPSHOT_DB_FILE_NAME,
        ipsw_meta_path=root / "ipsw_meta.json",
        ota_meta_path=root / "ota_image_meta.json",
        ipsw_blob_path=root / "ipsw_meta_blob.json",
        ota_blob_path=root / "ota_image_meta_blob.json",
    )


def read_manifest(cache_dir: Path) -> SnapshotManifest:
    path = manifest_path(cache_dir)
    if not path.exists():
        return SnapshotManifest()

    raw_payload: object = json.loads(path.read_text())
    if not isinstance(raw_payload, dict):
        return SnapshotManifest()

    payload = cast(dict[object, object], raw_payload)
    active_snapshot_id = payload.get("active_snapshot_id")
    if active_snapshot_id is None:
        return SnapshotManifest()
    return SnapshotManifest(active_snapshot_id=str(active_snapshot_id))


def write_manifest(cache_dir: Path, manifest: SnapshotManifest) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path(cache_dir).write_text(json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n")


def active_snapshot_paths(cache_dir: Path) -> SnapshotPaths | None:
    manifest = read_manifest(cache_dir)
    if manifest.active_snapshot_id is None:
        return None

    paths = snapshot_paths(cache_dir, manifest.active_snapshot_id)
    if not paths.db_path.exists():
        return None
    return paths


def prepare_snapshot_dir(paths: SnapshotPaths) -> None:
    if paths.root.exists():
        shutil.rmtree(paths.root)
    paths.root.mkdir(parents=True, exist_ok=True)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshot_info (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            snapshot_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            workflow_run_id INTEGER,
            workflow_run_url TEXT,
            ipsw_generation INTEGER NOT NULL,
            ota_generation INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ipsw_artifacts (
            artifact_key TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            version TEXT NOT NULL,
            build TEXT NOT NULL,
            released TEXT,
            release_status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ipsw_sources (
            artifact_key TEXT NOT NULL REFERENCES ipsw_artifacts(artifact_key) ON DELETE CASCADE,
            file_name TEXT NOT NULL,
            link TEXT NOT NULL,
            size INTEGER,
            sha1 TEXT,
            sha2 TEXT,
            devices_json TEXT NOT NULL,
            processing_state TEXT NOT NULL,
            mirror_path TEXT,
            last_run INTEGER NOT NULL,
            last_modified TEXT,
            PRIMARY KEY (artifact_key, link)
        );

        CREATE TABLE IF NOT EXISTS ota_artifacts (
            ota_key TEXT PRIMARY KEY,
            build TEXT NOT NULL,
            description_json TEXT NOT NULL,
            version TEXT NOT NULL,
            platform TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            url TEXT NOT NULL,
            devices_json TEXT NOT NULL,
            hash TEXT NOT NULL,
            hash_algorithm TEXT NOT NULL,
            processing_state TEXT NOT NULL,
            download_path TEXT,
            last_run INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS ipsw_sources_failure_idx
            ON ipsw_sources(processing_state, last_modified DESC);

        CREATE INDEX IF NOT EXISTS ota_artifacts_failure_idx
            ON ota_artifacts(processing_state, last_run DESC);
        """
    )


def build_snapshot_db(
    db_path: Path,
    snapshot_id: str,
    ipsw_db: IpswArtifactDb,
    ipsw_generation: int,
    ota_meta: dict[str, OtaArtifact],
    ota_generation: int,
    workflow_run_id: int | None,
    workflow_run_url: str | None,
) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM snapshot_info")
            conn.execute("DELETE FROM ipsw_sources")
            conn.execute("DELETE FROM ipsw_artifacts")
            conn.execute("DELETE FROM ota_artifacts")
            conn.execute(
                """
                INSERT INTO snapshot_info (
                    id,
                    snapshot_id,
                    created_at,
                    workflow_run_id,
                    workflow_run_url,
                    ipsw_generation,
                    ota_generation
                )
                VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    workflow_run_id,
                    workflow_run_url,
                    ipsw_generation,
                    ota_generation,
                ),
            )

            for artifact in ipsw_db.artifacts.values():
                conn.execute(
                    """
                    INSERT INTO ipsw_artifacts (
                        artifact_key,
                        platform,
                        version,
                        build,
                        released,
                        release_status
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.key,
                        artifact.platform.value,
                        artifact.version,
                        artifact.build,
                        artifact.released.isoformat() if artifact.released is not None else None,
                        artifact.release_status.value,
                    ),
                )
                for source in artifact.sources:
                    hashes = source.hashes
                    conn.execute(
                        """
                        INSERT INTO ipsw_sources (
                            artifact_key,
                            file_name,
                            link,
                            size,
                            sha1,
                            sha2,
                            devices_json,
                            processing_state,
                            mirror_path,
                            last_run,
                            last_modified
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            artifact.key,
                            source.file_name,
                            str(source.link),
                            source.size,
                            hashes.sha1 if hashes is not None else None,
                            hashes.sha2 if hashes is not None else None,
                            json.dumps(sorted(source.devices)),
                            source.processing_state.value,
                            source.mirror_path,
                            source.last_run,
                            source.last_modified.isoformat() if source.last_modified is not None else None,
                        ),
                    )

            for ota_key, artifact in ota_meta.items():
                conn.execute(
                    """
                    INSERT INTO ota_artifacts (
                        ota_key,
                        build,
                        description_json,
                        version,
                        platform,
                        artifact_id,
                        url,
                        devices_json,
                        hash,
                        hash_algorithm,
                        processing_state,
                        download_path,
                        last_run
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ota_key,
                        artifact.build,
                        json.dumps(sorted(artifact.description)),
                        artifact.version,
                        artifact.platform,
                        artifact.id,
                        artifact.url,
                        json.dumps(sorted(artifact.devices)),
                        artifact.hash,
                        artifact.hash_algorithm,
                        artifact.processing_state.value,
                        artifact.download_path,
                        artifact.last_run,
                    ),
                )
    finally:
        conn.close()


def load_snapshot_info(db_path: Path) -> SnapshotInfo | None:
    if not db_path.exists():
        return None

    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT snapshot_id, created_at, workflow_run_id, workflow_run_url, ipsw_generation, ota_generation
            FROM snapshot_info
            WHERE id = 1
            """
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return SnapshotInfo(
        snapshot_id=str(row["snapshot_id"]),
        created_at=str(row["created_at"]),
        workflow_run_id=int(row["workflow_run_id"]) if row["workflow_run_id"] is not None else None,
        workflow_run_url=str(row["workflow_run_url"]) if row["workflow_run_url"] is not None else None,
        ipsw_generation=int(row["ipsw_generation"]),
        ota_generation=int(row["ota_generation"]),
    )


def load_ipsw_rows(
    db_path: Path,
    states: tuple[ArtifactProcessingState, ...] | None = None,
    limit: int | None = None,
) -> list[IpswSourceRow]:
    if not db_path.exists() or states == ():
        return []

    query = """
        SELECT
            s.last_modified,
            s.processing_state,
            a.platform,
            a.version,
            a.build,
            s.artifact_key,
            s.file_name,
            s.link,
            s.sha1,
            s.last_run,
            s.mirror_path
        FROM ipsw_sources s
        JOIN ipsw_artifacts a ON a.artifact_key = s.artifact_key
    """
    params: list[str | int] = []
    if states is not None:
        placeholders = ", ".join("?" for _ in states)
        query = f"{query}\nWHERE s.processing_state IN ({placeholders})"
        params.extend(state.value for state in states)
    query = f"{query}\nORDER BY s.last_modified DESC, s.file_name ASC"
    if limit is not None:
        query = f"{query}\nLIMIT ?"
        params.append(limit)

    conn = connect(db_path)
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    return [
        IpswSourceRow(
            last_modified=_str_or_none(row["last_modified"]),
            processing_state=ArtifactProcessingState(str(row["processing_state"])),
            platform=str(row["platform"]),
            version=str(row["version"]),
            build=str(row["build"]),
            artifact_key=str(row["artifact_key"]),
            file_name=str(row["file_name"]),
            link=str(row["link"]),
            sha1=_str_or_none(row["sha1"]),
            last_run=int(row["last_run"]),
            mirror_path=_str_or_none(row["mirror_path"]),
        )
        for row in rows
    ]


def load_ipsw_failures(
    db_path: Path,
    failure_states: tuple[ArtifactProcessingState, ...] = DEFAULT_FAILURE_STATES,
    limit: int | None = None,
) -> list[IpswSourceRow]:
    return load_ipsw_rows(db_path, states=failure_states, limit=limit)


def load_ota_rows(
    db_path: Path,
    states: tuple[ArtifactProcessingState, ...] | None = None,
    limit: int | None = None,
) -> list[OtaArtifactRow]:
    if not db_path.exists() or states == ():
        return []

    query = """
        SELECT
            last_run,
            processing_state,
            platform,
            version,
            build,
            ota_key,
            artifact_id,
            url,
            hash,
            hash_algorithm,
            download_path
        FROM ota_artifacts
    """
    params: list[str | int] = []
    if states is not None:
        placeholders = ", ".join("?" for _ in states)
        query = f"{query}\nWHERE processing_state IN ({placeholders})"
        params.extend(state.value for state in states)
    query = f"{query}\nORDER BY last_run DESC, ota_key ASC"
    if limit is not None:
        query = f"{query}\nLIMIT ?"
        params.append(limit)

    conn = connect(db_path)
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    return [
        OtaArtifactRow(
            last_run=int(row["last_run"]),
            processing_state=ArtifactProcessingState(str(row["processing_state"])),
            platform=str(row["platform"]),
            version=str(row["version"]),
            build=str(row["build"]),
            ota_key=str(row["ota_key"]),
            artifact_id=str(row["artifact_id"]),
            url=str(row["url"]),
            hash=str(row["hash"]),
            hash_algorithm=str(row["hash_algorithm"]),
            download_path=_str_or_none(row["download_path"]),
        )
        for row in rows
    ]


def load_ota_failures(
    db_path: Path,
    failure_states: tuple[ArtifactProcessingState, ...] = DEFAULT_FAILURE_STATES,
    limit: int | None = None,
) -> list[OtaArtifactRow]:
    return load_ota_rows(db_path, states=failure_states, limit=limit)


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
