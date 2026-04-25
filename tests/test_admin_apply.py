from datetime import date, datetime

import pytest
from pydantic import HttpUrl

from symx.admin.actions import AdminActionKind, AdminStore, ApplyBatchRequest, IpswTarget, OtaTarget
from symx.admin.apply import AdminApplyValidationError, apply_request_to_ipsw_db, apply_request_to_ota_meta
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


def _make_ipsw_db() -> IpswArtifactDb:
    return IpswArtifactDb(
        artifacts={
            "iOS_18.0_22A100": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.0",
                build="22A100",
                released=date(2024, 9, 1),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    IpswSource(
                        devices=["iPhone17,1"],
                        link=HttpUrl("https://updates.cdn-apple.com/test.ipsw"),
                        hashes=IpswArtifactHashes(sha1="abc", sha2=None),
                        size=123,
                        processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        mirror_path="mirror/ipsw/iOS/18.0/22A100/test.ipsw",
                        last_run=111,
                        last_modified=datetime(2024, 9, 3, 12, 0, 0),
                    )
                ],
            )
        }
    )


def _make_ota_meta() -> dict[str, OtaArtifact]:
    return {
        "ota-key": OtaArtifact(
            build="22A100",
            description=["full"],
            version="18.0",
            platform="ios",
            id="ota-id",
            url="https://updates.cdn-apple.com/ota-id.zip",
            download_path="mirror/ota/ios/18.0/22A100/ota-id.zip",
            devices=["iPhone17,1"],
            hash="def",
            hash_algorithm="SHA-1",
            last_run=222,
            processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED,
        )
    }


def test_apply_request_to_ipsw_db_updates_processing_state_and_last_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    ipsw_db = _make_ipsw_db()
    request = ApplyBatchRequest(
        store=AdminStore.IPSW,
        action=AdminActionKind.QUEUE_EXTRACT,
        snapshot_id="ipsw-101__ota-202",
        base_generation=101,
        reason="retry extract",
        targets=(IpswTarget(artifact_key="iOS_18.0_22A100", link="https://updates.cdn-apple.com/test.ipsw"),),
    )

    applied_count = apply_request_to_ipsw_db(ipsw_db, request)

    source = ipsw_db.artifacts["iOS_18.0_22A100"].sources[0]
    assert applied_count == 1
    assert source.processing_state == ArtifactProcessingState.MIRRORED
    assert source.last_run == 999


def test_apply_request_to_ota_meta_updates_processing_state_and_last_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "444")
    ota_meta = _make_ota_meta()
    request = ApplyBatchRequest(
        store=AdminStore.OTA,
        action=AdminActionKind.QUEUE_MIRROR,
        snapshot_id="ipsw-101__ota-202",
        base_generation=202,
        reason="retry mirror",
        targets=(OtaTarget(ota_key="ota-key"),),
    )

    applied_count = apply_request_to_ota_meta(ota_meta, request)

    artifact = ota_meta["ota-key"]
    assert applied_count == 1
    assert artifact.processing_state == ArtifactProcessingState.INDEXED
    assert artifact.last_run == 444


def test_apply_request_validation_is_strict_for_missing_path_and_excluded_states() -> None:
    ipsw_db = _make_ipsw_db()
    ipsw_db.artifacts["iOS_18.0_22A100"].sources[0].mirror_path = None
    ota_meta = _make_ota_meta()
    ota_meta["ota-key"].processing_state = ArtifactProcessingState.RECOVERY_OTA

    ipsw_request = ApplyBatchRequest(
        store=AdminStore.IPSW,
        action=AdminActionKind.QUEUE_EXTRACT,
        snapshot_id="ipsw-101__ota-202",
        base_generation=101,
        reason="retry extract",
        targets=(IpswTarget(artifact_key="iOS_18.0_22A100", link="https://updates.cdn-apple.com/test.ipsw"),),
    )
    ota_request = ApplyBatchRequest(
        store=AdminStore.OTA,
        action=AdminActionKind.QUEUE_MIRROR,
        snapshot_id="ipsw-101__ota-202",
        base_generation=202,
        reason="retry mirror",
        targets=(OtaTarget(ota_key="ota-key"),),
    )

    with pytest.raises(AdminApplyValidationError, match="mirror_path is required"):
        apply_request_to_ipsw_db(ipsw_db, ipsw_request)

    with pytest.raises(AdminApplyValidationError, match="excluded"):
        apply_request_to_ota_meta(ota_meta, ota_request)
