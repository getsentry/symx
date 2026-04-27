import pytest

from symx.admin.actions import (
    AdminActionKind,
    AdminStore,
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyBatchStatus,
    IpswTarget,
    OtaTarget,
    PendingBatch,
    WorkerDispatchResult,
    WorkerDispatchStatus,
    bind_pending_batch,
    preview_action,
    preview_target_against_snapshot,
    snapshot_generation_for_store,
    validate_pending_batch_against_snapshot,
)
from symx.admin.db import IpswSourceRow, OtaArtifactRow, SnapshotInfo
from symx.model import ArtifactProcessingState


def test_preview_action_requires_existing_path_for_extract_and_blocks_curated_exclusions() -> None:
    missing_path = preview_action(
        AdminStore.IPSW,
        AdminActionKind.QUEUE_EXTRACT,
        ArtifactProcessingState.SYMBOLS_EXTRACTED,
        has_required_path=False,
    )
    excluded = preview_action(
        AdminStore.OTA,
        AdminActionKind.QUEUE_MIRROR,
        ArtifactProcessingState.RECOVERY_OTA,
        has_required_path=True,
    )
    already_eligible = preview_action(
        AdminStore.OTA,
        AdminActionKind.QUEUE_MIRROR,
        ArtifactProcessingState.INDEXED,
        has_required_path=False,
    )

    assert missing_path.allowed is False
    assert "mirror_path" in missing_path.note
    assert excluded.allowed is False
    assert "excluded" in excluded.note
    assert already_eligible.allowed is True
    assert already_eligible.resulting_state == ArtifactProcessingState.INDEXED
    assert "already eligible" in already_eligible.note


def test_bind_pending_batch_uses_store_generation_and_requires_reason() -> None:
    snapshot = SnapshotInfo(
        snapshot_id="ipsw-101__ota-202",
        created_at="2024-09-03T12:00:00+00:00",
        workflow_run_id=1,
        workflow_run_url=None,
        ipsw_generation=101,
        ota_generation=202,
    )
    batch = PendingBatch(
        store=AdminStore.IPSW,
        action=AdminActionKind.QUEUE_EXTRACT,
        targets=(IpswTarget(artifact_key="iOS_18.0_22A100", link="https://example.invalid/test.ipsw"),),
        reason="retry after extractor fix",
    )

    request = bind_pending_batch(batch, snapshot)

    assert request.snapshot_id == snapshot.snapshot_id
    assert request.base_generation == snapshot_generation_for_store(snapshot, AdminStore.IPSW)
    assert request.reason == batch.reason


def test_apply_batch_request_and_result_round_trip() -> None:
    request = ApplyBatchRequest(
        store=AdminStore.OTA,
        action=AdminActionKind.QUEUE_MIRROR,
        snapshot_id="ipsw-11__ota-22",
        base_generation=22,
        reason="retry mirror",
        targets=(OtaTarget(ota_key="ota-key"),),
    )
    result = ApplyBatchResult(
        status=ApplyBatchStatus.APPLIED_WITH_WORKER_WARNING,
        store=request.store,
        action=request.action,
        snapshot_id=request.snapshot_id,
        base_generation=request.base_generation,
        remote_generation=22,
        targets=request.targets,
        reason=request.reason,
        applied_count=1,
        message="Applied with a warning",
        worker=WorkerDispatchResult(
            workflow="symx-ota-mirror.yml",
            status=WorkerDispatchStatus.DISPATCH_FAILED,
            detail="gh failed",
        ),
    )

    assert ApplyBatchRequest.from_json(request.to_json()) == request
    assert ApplyBatchResult.from_json(result.to_json()) == result


def test_apply_batch_request_rejects_boolean_integer_payloads() -> None:
    with pytest.raises(ValueError, match="Unexpected integer payload"):
        ApplyBatchRequest.from_json(
            '{"store":"ota","action":"queue_mirror","snapshot_id":"ipsw-1__ota-2","base_generation":true,'
            '"reason":"retry mirror","targets":[{"ota_key":"ota-key"}]}'
        )


def test_preview_target_against_snapshot_uses_snapshot_rows_and_blocks_missing_extract_path() -> None:
    ipsw_row = IpswSourceRow(
        last_modified="2024-09-03T12:34:56",
        processing_state=ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
        platform="iOS",
        version="18.0",
        build="22A100",
        artifact_key="iOS_18.0_22A100",
        file_name="test.ipsw",
        link="https://updates.cdn-apple.com/test.ipsw",
        sha1="abc123",
        last_run=123,
        mirror_path="mirror/ipsw/iOS/18.0/22A100/test.ipsw",
    )
    ota_row = OtaArtifactRow(
        last_run=456,
        processing_state=ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
        platform="ios",
        version="18.0",
        build="22A100",
        ota_key="ota-key",
        artifact_id="ota-id",
        url="https://updates.cdn-apple.com/test.zip",
        hash="def456",
        hash_algorithm="SHA-1",
        download_path=None,
    )

    ipsw_preview = preview_target_against_snapshot(
        AdminStore.IPSW,
        AdminActionKind.QUEUE_EXTRACT,
        IpswTarget(artifact_key=ipsw_row.artifact_key, link=ipsw_row.link),
        {f"{ipsw_row.artifact_key}::{ipsw_row.link}": ipsw_row},
        {},
    )
    ota_preview = preview_target_against_snapshot(
        AdminStore.OTA,
        AdminActionKind.QUEUE_EXTRACT,
        OtaTarget(ota_key=ota_row.ota_key),
        {},
        {ota_row.ota_key: ota_row},
    )

    assert ipsw_preview.allowed is True
    assert ipsw_preview.current_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
    assert ipsw_preview.resulting_state == ArtifactProcessingState.MIRRORED
    assert ipsw_preview.row_label == "test.ipsw"
    assert ota_preview.allowed is False
    assert ota_preview.current_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
    assert ota_preview.resulting_state is None
    assert ota_preview.row_label == "ota-id"
    assert ota_preview.note == "download_path is required to queue extract"


def test_preview_target_against_snapshot_uses_ipsw_mirror_path_label_for_extract_errors() -> None:
    ipsw_row = IpswSourceRow(
        last_modified="2024-09-03T12:34:56",
        processing_state=ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
        platform="iOS",
        version="18.0",
        build="22A100",
        artifact_key="iOS_18.0_22A100",
        file_name="test.ipsw",
        link="https://updates.cdn-apple.com/test.ipsw",
        sha1="abc123",
        last_run=123,
        mirror_path=None,
    )

    ipsw_preview = preview_target_against_snapshot(
        AdminStore.IPSW,
        AdminActionKind.QUEUE_EXTRACT,
        IpswTarget(artifact_key=ipsw_row.artifact_key, link=ipsw_row.link),
        {f"{ipsw_row.artifact_key}::{ipsw_row.link}": ipsw_row},
        {},
    )

    assert ipsw_preview.allowed is False
    assert ipsw_preview.current_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
    assert ipsw_preview.resulting_state is None
    assert ipsw_preview.row_label == "test.ipsw"
    assert ipsw_preview.note == "mirror_path is required to queue extract"


def test_validate_pending_batch_against_snapshot_reports_missing_rows() -> None:
    batch = PendingBatch(
        store=AdminStore.OTA,
        action=AdminActionKind.QUEUE_EXTRACT,
        targets=(OtaTarget(ota_key="missing-ota-key"),),
        reason="retry extract",
    )

    issues = validate_pending_batch_against_snapshot(batch, {}, {})

    assert len(issues) == 1
    assert issues[0].target == "missing-ota-key"
    assert issues[0].reason == "missing from current snapshot"
