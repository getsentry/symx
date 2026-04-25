from datetime import date, datetime
from pathlib import Path

from pydantic import HttpUrl

from symx.admin.db import (
    DEFAULT_FAILURE_STATES,
    SnapshotManifest,
    build_snapshot_db,
    load_ipsw_failures,
    load_ipsw_rows,
    load_ota_failures,
    load_ota_rows,
    load_snapshot_info,
    make_snapshot_id,
    read_manifest,
    snapshot_paths,
    write_manifest,
)
from symx.ipsw.model import (
    IpswArtifact,
    IpswArtifactDb,
    IpswArtifactHashes,
    IpswPlatform,
    IpswReleaseStatus,
    IpswSource,
)
from symx.model import ArtifactProcessingState
from symx.ota.model import OtaArtifact


def _ipsw_source(
    file_name: str,
    processing_state: ArtifactProcessingState,
    last_run: int,
    last_modified: datetime,
    link: str | None = None,
) -> IpswSource:
    source = IpswSource(
        devices=["iPhone17,1"],
        link=HttpUrl(link or f"https://updates.cdn-apple.com/{file_name}"),
        hashes=IpswArtifactHashes(sha1=f"sha1-{file_name}", sha2=None),
        size=123,
        processing_state=processing_state,
        mirror_path=f"mirror/{file_name}" if processing_state == ArtifactProcessingState.MIRRORED else None,
        last_run=last_run,
        last_modified=last_modified,
    )
    return source


def _ota_artifact(
    artifact_id: str,
    processing_state: ArtifactProcessingState,
    last_run: int,
) -> OtaArtifact:
    return OtaArtifact(
        build="22A100",
        description=["full"],
        version="18.0",
        platform="ios",
        id=artifact_id,
        url=f"https://updates.cdn-apple.com/{artifact_id}.zip",
        download_path=None,
        devices=["iPhone17,1"],
        hash=f"hash-{artifact_id}",
        hash_algorithm="SHA-1",
        last_run=last_run,
        processing_state=processing_state,
    )


def test_build_snapshot_db_and_query_failures(tmp_path: Path) -> None:
    snapshot_id = make_snapshot_id(101, 202)
    paths = snapshot_paths(tmp_path, snapshot_id)
    paths.root.mkdir(parents=True)

    ipsw_db = IpswArtifactDb(
        artifacts={
            "iOS_18.0_22A100": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.0",
                build="22A100",
                released=date(2024, 9, 1),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    _ipsw_source(
                        "newest.ipsw",
                        ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
                        last_run=300,
                        last_modified=datetime(2024, 9, 3, 12, 0, 0),
                    ),
                    _ipsw_source(
                        "newest.ipsw",
                        ArtifactProcessingState.MIRRORING_FAILED,
                        last_run=200,
                        last_modified=datetime(2024, 9, 2, 12, 0, 0),
                        link="https://updates.cdn-apple.com/alternate/newest.ipsw",
                    ),
                    _ipsw_source(
                        "ok.ipsw",
                        ArtifactProcessingState.MIRRORED,
                        last_run=100,
                        last_modified=datetime(2024, 9, 1, 12, 0, 0),
                    ),
                ],
            )
        }
    )
    ota_meta = {
        "ota-newest": _ota_artifact("ota-newest", ArtifactProcessingState.INDEXED_INVALID, last_run=400),
        "ota-older": _ota_artifact("ota-older", ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED, last_run=300),
        "ota-ok": _ota_artifact("ota-ok", ArtifactProcessingState.MIRRORED, last_run=100),
    }

    build_snapshot_db(
        paths.db_path,
        snapshot_id,
        ipsw_db,
        ipsw_generation=101,
        ota_meta=ota_meta,
        ota_generation=202,
        workflow_run_id=999,
        workflow_run_url="https://github.example/run/999",
    )

    snapshot_info = load_snapshot_info(paths.db_path)
    assert snapshot_info is not None
    assert snapshot_info.snapshot_id == snapshot_id
    assert snapshot_info.workflow_run_id == 999
    assert snapshot_info.ipsw_generation == 101
    assert snapshot_info.ota_generation == 202

    ipsw_failures = load_ipsw_failures(paths.db_path)
    assert [row.file_name for row in ipsw_failures] == ["newest.ipsw", "newest.ipsw"]
    assert [row.link for row in ipsw_failures] == [
        "https://updates.cdn-apple.com/newest.ipsw",
        "https://updates.cdn-apple.com/alternate/newest.ipsw",
    ]
    assert [row.processing_state for row in ipsw_failures] == [
        ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
        ArtifactProcessingState.MIRRORING_FAILED,
    ]

    ota_failures = load_ota_failures(paths.db_path)
    assert [row.ota_key for row in ota_failures] == ["ota-newest", "ota-older"]
    assert [row.last_run for row in ota_failures] == [400, 300]


def test_row_queries_return_all_rows_by_default(tmp_path: Path) -> None:
    snapshot_id = make_snapshot_id(303, 404)
    paths = snapshot_paths(tmp_path, snapshot_id)
    paths.root.mkdir(parents=True)

    ipsw_sources = [
        _ipsw_source(
            f"failure-{index:02d}.ipsw",
            ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
            last_run=1000 + index,
            last_modified=datetime(2024, 9, index + 1, 12, 0, 0),
        )
        for index in range(12)
    ]
    ipsw_db = IpswArtifactDb(
        artifacts={
            "iOS_18.1_22B100": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.1",
                build="22B100",
                released=date(2024, 10, 1),
                release_status=IpswReleaseStatus.RELEASE,
                sources=ipsw_sources,
            )
        }
    )
    ota_meta = {
        f"ota-{index:02d}": _ota_artifact(
            f"ota-{index:02d}",
            ArtifactProcessingState.INDEXED_INVALID,
            last_run=2000 + index,
        )
        for index in range(12)
    }

    build_snapshot_db(
        paths.db_path,
        snapshot_id,
        ipsw_db,
        ipsw_generation=303,
        ota_meta=ota_meta,
        ota_generation=404,
        workflow_run_id=1001,
        workflow_run_url="https://github.example/run/1001",
    )

    assert len(load_ipsw_rows(paths.db_path)) == 12
    assert len(load_ota_rows(paths.db_path)) == 12
    assert len(load_ipsw_rows(paths.db_path, limit=5)) == 5
    assert len(load_ota_rows(paths.db_path, limit=5)) == 5


def test_default_failure_states_track_currently_emitted_states() -> None:
    assert DEFAULT_FAILURE_STATES == (
        ArtifactProcessingState.MIRRORING_FAILED,
        ArtifactProcessingState.MIRROR_CORRUPT,
        ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
        ArtifactProcessingState.INDEXED_INVALID,
    )


def test_manifest_round_trip(tmp_path: Path) -> None:
    assert read_manifest(tmp_path).active_snapshot_id is None

    write_manifest(tmp_path, SnapshotManifest(active_snapshot_id="ipsw-1__ota-2"))

    manifest = read_manifest(tmp_path)
    assert manifest.active_snapshot_id == "ipsw-1__ota-2"
