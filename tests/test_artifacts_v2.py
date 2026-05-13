from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import HttpUrl, ValidationError

from symx.artifacts.convert import convert_ipsw_db, convert_ota_meta, ota_filename
from symx.artifacts.ids import ipsw_artifact_uid, ota_artifact_uid
from symx.artifacts.model import ArtifactBundle, ArtifactKind, ArtifactSourceKind, LegacyStore
from symx.artifacts.report import build_parity_report
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


def make_ipsw_source(
    link: str,
    state: ArtifactProcessingState = ArtifactProcessingState.INDEXED,
    mirror_path: str | None = None,
) -> IpswSource:
    return IpswSource(
        devices=["iPhone14,7", "iPhone15,2"],
        link=HttpUrl(link),
        hashes=IpswArtifactHashes(sha1="abc123", sha2=None),
        size=1234,
        processing_state=state,
        mirror_path=mirror_path,
        last_run=42,
        last_modified=datetime(2026, 1, 2, 3, 4, 5),
    )


def make_ipsw_artifact(sources: list[IpswSource]) -> IpswArtifact:
    return IpswArtifact(
        platform=IpswPlatform.IOS,
        version="18.2",
        build="22C152",
        released=date.today(),
        release_status=IpswReleaseStatus.RELEASE,
        sources=sources,
    )


def make_ota_artifact(
    state: ArtifactProcessingState = ArtifactProcessingState.INDEXED,
    download_path: str | None = None,
    version: str = "18.2",
) -> OtaArtifact:
    return OtaArtifact(
        build="22C152",
        description=["iOS 18.2"],
        version=version,
        platform="ios",
        id="a" * 40,
        url=f"https://updates.apple.com/{'a' * 40}.zip",
        download_path=download_path,
        devices=["iPhone14,7"],
        hash="def456",
        hash_algorithm="SHA-1",
        last_run=77,
        processing_state=state,
    )


def test_artifact_uids_are_stable_and_domain_prefixed() -> None:
    assert ipsw_artifact_uid("iOS_18.2_22C152", "https://example.com/a.ipsw") == ipsw_artifact_uid(
        "iOS_18.2_22C152", "https://example.com/a.ipsw"
    )
    assert ipsw_artifact_uid("iOS_18.2_22C152", "https://example.com/a.ipsw").startswith("ipsw:")
    assert ota_artifact_uid("ota-key").startswith("ota:")
    assert ipsw_artifact_uid("shared", "key") != ota_artifact_uid("shared")


def test_convert_ipsw_db_creates_one_artifact_per_source_and_round_trips_json() -> None:
    first = make_ipsw_source("https://example.com/first.ipsw", ArtifactProcessingState.MIRRORED, "mirror/ipsw/first")
    second = make_ipsw_source("https://example.com/second.ipsw")
    artifact = make_ipsw_artifact([first, second])
    bundles = convert_ipsw_db(IpswArtifactDb(artifacts={artifact.key: artifact}))

    assert len(bundles) == 2
    record = bundles[0].artifact
    detail = bundles[0].ipsw_detail
    assert record.kind == ArtifactKind.IPSW
    assert record.source_kind == ArtifactSourceKind.APPLEDB
    assert record.platform == "iOS"
    assert record.version == "18.2"
    assert record.build == "22C152"
    assert record.filename == "first.ipsw"
    assert record.hash_algorithm == "sha1"
    assert record.hash_value == "abc123"
    assert record.mirror_path == "mirror/ipsw/first"
    assert record.processing_state == ArtifactProcessingState.MIRRORED
    assert record.symbol_store_prefix == "ios"
    assert record.symbol_bundle_id == "ipsw_first"
    assert record.symbol_bundle_id != record.artifact_uid
    assert record.legacy.store == LegacyStore.IPSW
    assert record.legacy.artifact_key == artifact.key
    assert record.legacy.source_link == str(first.link)
    assert detail is not None
    assert detail.source_index == 0
    assert detail.devices == ["iPhone14,7", "iPhone15,2"]

    round_tripped = ArtifactBundle.model_validate_json(bundles[0].model_dump_json())
    assert round_tripped == bundles[0]


def test_convert_ota_meta_creates_artifact_and_detail() -> None:
    ota = make_ota_artifact(ArtifactProcessingState.MIRRORED, "mirror/ota/ios/18.2/22C152/file.zip")
    bundles = convert_ota_meta({"ota-key": ota})

    assert len(bundles) == 1
    record = bundles[0].artifact
    detail = bundles[0].ota_detail
    assert record.kind == ArtifactKind.OTA
    assert record.source_kind == ArtifactSourceKind.APPLE_OTA_FEED
    assert record.platform == "ios"
    assert record.source_key == "ota-key"
    assert record.filename == ota_filename(ota)
    assert record.hash_algorithm == "SHA-1"
    assert record.hash_value == "def456"
    assert record.mirror_path == "mirror/ota/ios/18.2/22C152/file.zip"
    assert record.symbol_store_prefix == "ios"
    assert record.symbol_bundle_id == "ota_ota-key"
    assert record.symbol_bundle_id != record.artifact_uid
    assert record.legacy.store == LegacyStore.OTA
    assert record.legacy.ota_key == "ota-key"
    assert detail is not None
    assert detail.ota_id == ota.id
    assert detail.description == ["iOS 18.2"]


def test_artifact_bundle_requires_exactly_one_matching_detail() -> None:
    ota_bundle = convert_ota_meta({"ota-key": make_ota_artifact()})[0]
    with pytest.raises(ValidationError):
        ArtifactBundle(artifact=ota_bundle.artifact)


def test_parity_report_counts_states_and_worklist_matches() -> None:
    ipsw_artifact = make_ipsw_artifact(
        [
            make_ipsw_source("https://example.com/indexed.ipsw", ArtifactProcessingState.INDEXED),
            make_ipsw_source(
                "https://example.com/mirrored.ipsw",
                ArtifactProcessingState.MIRRORED,
                "mirror/ipsw/mirrored",
            ),
        ]
    )
    ota_indexed = make_ota_artifact(ArtifactProcessingState.INDEXED)
    ota_mirrored = make_ota_artifact(ArtifactProcessingState.MIRRORED, "mirror/ota/mirrored", version="18.3")

    report = build_parity_report(
        IpswArtifactDb(artifacts={ipsw_artifact.key: ipsw_artifact}),
        {"ota-indexed": ota_indexed, "ota-mirrored": ota_mirrored},
    )

    assert report.ok
    assert report.total_artifacts == 4
    assert report.totals_by_kind == {"ipsw": 2, "ota": 2}
    assert report.state_counts_by_kind["ipsw"] == {"indexed": 1, "mirrored": 1}
    assert report.state_counts_by_kind["ota"] == {"indexed": 1, "mirrored": 1}
    assert report.worklists["ipsw_mirror"].legacy_count == 1
    assert report.worklists["ipsw_extract"].legacy_count == 1
    assert report.worklists["ota_mirror"].legacy_count == 1
    assert report.worklists["ota_extract"].legacy_count == 1
    assert all(worklist.matches for worklist in report.worklists.values())
