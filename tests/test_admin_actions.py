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
    snapshot_generation_for_store,
)
from symx.admin.db import SnapshotInfo
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
