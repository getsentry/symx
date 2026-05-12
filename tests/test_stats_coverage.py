from datetime import date, datetime
from pathlib import Path

from pydantic import HttpUrl

from symx.admin.db import SnapshotManifest, build_snapshot_db, make_snapshot_id, snapshot_paths, write_manifest
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
from symx.stats.coverage import build_coverage_report, render_coverage_html, resolve_snapshot_db


def _ipsw_source(
    file_name: str,
    processing_state: ArtifactProcessingState,
    last_run: int,
    last_modified: datetime,
    link: str | None = None,
) -> IpswSource:
    return IpswSource(
        devices=["iPhone17,1"],
        link=HttpUrl(link or f"https://updates.cdn-apple.com/{file_name}"),
        hashes=IpswArtifactHashes(sha1=f"sha1-{file_name}", sha2=None),
        size=123,
        processing_state=processing_state,
        mirror_path="mirror/with-symbols" if processing_state == ArtifactProcessingState.SYMBOLS_EXTRACTED else None,
        last_run=last_run,
        last_modified=last_modified,
    )


def _ota_artifact(
    artifact_id: str,
    *,
    version: str,
    platform: str,
    processing_state: ArtifactProcessingState,
    last_run: int,
) -> OtaArtifact:
    return OtaArtifact(
        build="22A100",
        description=["full"],
        version=version,
        platform=platform,
        id=artifact_id,
        url=f"https://updates.cdn-apple.com/{artifact_id}.zip",
        download_path="mirror/ota/with-symbols"
        if processing_state == ArtifactProcessingState.SYMBOLS_EXTRACTED
        else None,
        devices=["iPhone17,1"],
        hash=f"hash-{artifact_id}",
        hash_algorithm="SHA-1",
        last_run=last_run,
        processing_state=processing_state,
    )


def test_build_coverage_report_groups_counts_and_sorts_rows(tmp_path: Path) -> None:
    snapshot_id = make_snapshot_id(101, 202)
    paths = snapshot_paths(tmp_path, snapshot_id)
    paths.root.mkdir(parents=True)

    ipsw_db = IpswArtifactDb(
        artifacts={
            "ios-181": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.1",
                build="22B100",
                released=date(2024, 10, 1),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    _ipsw_source(
                        "ios-181.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=10,
                        last_modified=datetime(2024, 10, 1, 12, 0, 0),
                    )
                ],
            ),
            "ios-180-rel": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.0",
                build="22A100",
                released=date(2024, 9, 1),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    _ipsw_source(
                        "ios-180-a.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=20,
                        last_modified=datetime(2024, 9, 2, 12, 0, 0),
                    ),
                    _ipsw_source(
                        "ios-180-b.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=21,
                        last_modified=datetime(2024, 9, 2, 12, 5, 0),
                    ),
                    _ipsw_source(
                        "ios-180-mirrored.ipsw",
                        ArtifactProcessingState.MIRRORED,
                        last_run=22,
                        last_modified=datetime(2024, 9, 2, 12, 10, 0),
                    ),
                ],
            ),
            "ios-180-beta": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.0_beta",
                build="22A5307g",
                released=date(2024, 8, 1),
                release_status=IpswReleaseStatus.BETA,
                sources=[
                    _ipsw_source(
                        "ios-180-beta.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=30,
                        last_modified=datetime(2024, 8, 2, 12, 0, 0),
                    )
                ],
            ),
            "ios-180-rel-second-build": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.0",
                build="22A101",
                released=date(2024, 9, 2),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    _ipsw_source(
                        "ios-180-c.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=31,
                        last_modified=datetime(2024, 9, 2, 12, 20, 0),
                    )
                ],
            ),
            "macos-150-rc": IpswArtifact(
                platform=IpswPlatform.MACOS,
                version="15.0_RC",
                build="24A335",
                released=date(2024, 7, 1),
                release_status=IpswReleaseStatus.RELEASE_CANDIDATE,
                sources=[
                    _ipsw_source(
                        "macos-150-rc.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=40,
                        last_modified=datetime(2024, 7, 2, 12, 0, 0),
                    )
                ],
            ),
        }
    )
    ota_meta = {
        "ota-ios-181-a": _ota_artifact(
            "ota-ios-181-a",
            version="18.1",
            platform="ios",
            processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED,
            last_run=100,
        ),
        "ota-ios-181-b": _ota_artifact(
            "ota-ios-181-b",
            version="18.1",
            platform="ios",
            processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED,
            last_run=101,
        ),
        "ota-ios-180": _ota_artifact(
            "ota-ios-180",
            version="18.0",
            platform="ios",
            processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED,
            last_run=102,
        ),
        "ota-macos-150": _ota_artifact(
            "ota-macos-150",
            version="15.0",
            platform="macos",
            processing_state=ArtifactProcessingState.MIRRORED,
            last_run=103,
        ),
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

    report = build_coverage_report(paths.db_path)

    assert [(row.platform, row.version, row.count) for row in report.ipsw_rows] == [
        ("iOS", "18.1", 1),
        ("iOS", "18.0", 3),
        ("iOS", "18.0_beta", 1),
        ("macOS", "15.0_RC", 1),
    ]
    assert [(row.platform, row.version, row.count) for row in report.ota_rows] == [
        ("ios", "18.1", 2),
        ("ios", "18.0", 1),
    ]
    assert report.ipsw_total_count == 6
    assert report.ota_total_count == 3


def test_build_coverage_report_orders_descending_version_parts_with_release_train_tiebreakers(tmp_path: Path) -> None:
    snapshot_id = make_snapshot_id(505, 606)
    paths = snapshot_paths(tmp_path, snapshot_id)
    paths.root.mkdir(parents=True)

    ipsw_db = IpswArtifactDb(
        artifacts={
            "ios-261": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="26.1",
                build="23B74",
                released=date(2025, 10, 15),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    _ipsw_source(
                        "ios-261.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=1,
                        last_modified=datetime(2025, 10, 15, 12, 0, 0),
                    )
                ],
            ),
            "ios-261-rc": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="26.1_RC",
                build="23B5059e",
                released=date(2025, 10, 1),
                release_status=IpswReleaseStatus.RELEASE_CANDIDATE,
                sources=[
                    _ipsw_source(
                        "ios-261-rc.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=2,
                        last_modified=datetime(2025, 10, 1, 12, 0, 0),
                    )
                ],
            ),
            "ios-261-beta": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="26.1_beta",
                build="23B5045f",
                released=date(2025, 9, 15),
                release_status=IpswReleaseStatus.BETA,
                sources=[
                    _ipsw_source(
                        "ios-261-beta.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=3,
                        last_modified=datetime(2025, 9, 15, 12, 0, 0),
                    )
                ],
            ),
            "ios-260-beta9": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="26.0_beta_9",
                build="23A5346a",
                released=date(2025, 8, 1),
                release_status=IpswReleaseStatus.BETA,
                sources=[
                    _ipsw_source(
                        "ios-260-beta9.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=4,
                        last_modified=datetime(2025, 8, 1, 12, 0, 0),
                    )
                ],
            ),
            "ios-2601": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="26.0.1",
                build="23A340",
                released=date(2025, 9, 1),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    _ipsw_source(
                        "ios-2601.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=5,
                        last_modified=datetime(2025, 9, 1, 12, 0, 0),
                    )
                ],
            ),
            "ios-260": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="26.0",
                build="23A339",
                released=date(2025, 8, 20),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    _ipsw_source(
                        "ios-260.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=6,
                        last_modified=datetime(2025, 8, 20, 12, 0, 0),
                    )
                ],
            ),
            "ios-260-beta": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="26.0_beta",
                build="23A5276f",
                released=date(2025, 7, 1),
                release_status=IpswReleaseStatus.BETA,
                sources=[
                    _ipsw_source(
                        "ios-260-beta.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=7,
                        last_modified=datetime(2025, 7, 1, 12, 0, 0),
                    )
                ],
            ),
            "ios-260-rc": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="26.0_RC",
                build="23A5330a",
                released=date(2025, 8, 10),
                release_status=IpswReleaseStatus.RELEASE_CANDIDATE,
                sources=[
                    _ipsw_source(
                        "ios-260-rc.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=8,
                        last_modified=datetime(2025, 8, 10, 12, 0, 0),
                    )
                ],
            ),
        }
    )

    build_snapshot_db(
        paths.db_path,
        snapshot_id,
        ipsw_db,
        ipsw_generation=505,
        ota_meta={},
        ota_generation=606,
        workflow_run_id=1002,
        workflow_run_url="https://github.example/run/1002",
    )

    report = build_coverage_report(paths.db_path)

    assert [(row.platform, row.version) for row in report.ipsw_rows] == [
        ("iOS", "26.1"),
        ("iOS", "26.1_RC"),
        ("iOS", "26.1_beta"),
        ("iOS", "26.0.1"),
        ("iOS", "26.0"),
        ("iOS", "26.0_RC"),
        ("iOS", "26.0_beta_9"),
        ("iOS", "26.0_beta"),
    ]


def test_render_coverage_html_includes_snapshot_metadata_and_counts(tmp_path: Path) -> None:
    snapshot_id = make_snapshot_id(303, 404)
    paths = snapshot_paths(tmp_path, snapshot_id)
    paths.root.mkdir(parents=True)

    ipsw_db = IpswArtifactDb(
        artifacts={
            "ios-180": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.0_beta",
                build="22A5307g",
                released=date(2024, 8, 1),
                release_status=IpswReleaseStatus.BETA,
                sources=[
                    _ipsw_source(
                        "ios-180-beta.ipsw",
                        ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        last_run=1,
                        last_modified=datetime(2024, 8, 2, 12, 0, 0),
                    )
                ],
            )
        }
    )
    ota_meta = {
        "ota-ios-180": _ota_artifact(
            "ota-ios-180",
            version="18.0",
            platform="ios",
            processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED,
            last_run=2,
        )
    }

    build_snapshot_db(
        paths.db_path,
        snapshot_id,
        ipsw_db,
        ipsw_generation=303,
        ota_meta=ota_meta,
        ota_generation=404,
        workflow_run_id=111,
        workflow_run_url="https://github.example/run/111",
    )
    write_manifest(tmp_path, SnapshotManifest(active_snapshot_id=snapshot_id))

    report = build_coverage_report(resolve_snapshot_db(tmp_path))
    html = render_coverage_html(report)

    assert "Symx coverage stats" in html
    assert "processing_state = symbols_extracted" in html
    assert snapshot_id in html
    assert "https://github.example/run/111" in html
    assert "18.0 beta" in html
    assert 'id="coverage-data"' in html
    assert 'href="#ipsw-section"' in html
    assert 'href="#ota-section"' in html
    assert 'href="#top"' in html
    assert 'id="ipsw-platform"' in html
    assert 'id="ipsw-major"' in html
    assert 'id="ipsw-minor"' in html
    assert 'id="ipsw-patch"' in html
    assert 'id="ipsw-reset"' in html
    assert 'id="ota-platform"' in html
    assert 'id="ota-reset"' in html
    assert "Reset filters" in html
    assert '"baseParts":[18,0]' in html
    assert '"patch":null' in html
    assert "{{" not in html
    assert html.count("Count: <strong>1</strong>") == 2
