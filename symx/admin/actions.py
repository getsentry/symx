from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import cast

from symx.admin.db import IpswSourceRow, OtaArtifactRow, SnapshotInfo
from symx.model import ArtifactProcessingState


class AdminStore(StrEnum):
    IPSW = "ipsw"
    OTA = "ota"


class AdminActionKind(StrEnum):
    QUEUE_MIRROR = "queue_mirror"
    QUEUE_EXTRACT = "queue_extract"


class ApplyBatchStatus(StrEnum):
    APPLIED = "applied"
    APPLIED_WITH_WORKER_WARNING = "applied_with_worker_warning"
    STALE_GENERATION = "stale_generation"
    VALIDATION_FAILED = "validation_failed"
    INTERNAL_ERROR = "internal_error"


class WorkerDispatchStatus(StrEnum):
    ALREADY_RUNNING = "already_running"
    DISPATCHED = "dispatched"
    DISPATCH_FAILED = "dispatch_failed"


@dataclass(frozen=True)
class IpswTarget:
    artifact_key: str
    link: str


@dataclass(frozen=True)
class OtaTarget:
    ota_key: str


AdminTarget = IpswTarget | OtaTarget


@dataclass(frozen=True)
class PendingBatch:
    store: AdminStore
    action: AdminActionKind
    targets: tuple[AdminTarget, ...]
    reason: str = ""


@dataclass(frozen=True)
class ApplyBatchRequest:
    store: AdminStore
    action: AdminActionKind
    snapshot_id: str
    base_generation: int
    targets: tuple[AdminTarget, ...]
    reason: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, payload_raw: str) -> ApplyBatchRequest:
        payload: dict[str, object] = json.loads(payload_raw)
        store = AdminStore(str(payload["store"]))
        action = AdminActionKind(str(payload["action"]))
        snapshot_id = str(payload["snapshot_id"])
        base_generation = _coerce_int(payload["base_generation"])
        reason = str(payload["reason"])
        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, list):
            raise ValueError("Unexpected targets payload")

        return cls(
            store=store,
            action=action,
            snapshot_id=snapshot_id,
            base_generation=base_generation,
            targets=_parse_targets(store, cast(list[object], raw_targets)),
            reason=reason,
        )


@dataclass(frozen=True)
class WorkerDispatchResult:
    workflow: str
    status: WorkerDispatchStatus
    detail: str | None = None


@dataclass(frozen=True)
class ApplyBatchResult:
    status: ApplyBatchStatus
    store: AdminStore
    action: AdminActionKind
    snapshot_id: str
    base_generation: int
    remote_generation: int
    targets: tuple[AdminTarget, ...]
    reason: str
    applied_count: int
    message: str
    worker: WorkerDispatchResult | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, payload_raw: str) -> ApplyBatchResult:
        payload: dict[str, object] = json.loads(payload_raw)
        store = AdminStore(str(payload["store"]))
        worker_payload = payload.get("worker")
        worker: WorkerDispatchResult | None = None
        if isinstance(worker_payload, dict):
            worker_dict = cast(dict[str, object], worker_payload)
            worker = WorkerDispatchResult(
                workflow=str(worker_dict["workflow"]),
                status=WorkerDispatchStatus(str(worker_dict["status"])),
                detail=_optional_str(worker_dict.get("detail")),
            )

        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, list):
            raise ValueError("Unexpected targets payload")

        return cls(
            status=ApplyBatchStatus(str(payload["status"])),
            store=store,
            action=AdminActionKind(str(payload["action"])),
            snapshot_id=str(payload["snapshot_id"]),
            base_generation=_coerce_int(payload["base_generation"]),
            remote_generation=_coerce_int(payload["remote_generation"]),
            targets=_parse_targets(store, cast(list[object], raw_targets)),
            reason=str(payload["reason"]),
            applied_count=_coerce_int(payload["applied_count"]),
            message=str(payload["message"]),
            worker=worker,
        )


@dataclass(frozen=True)
class ActionPreview:
    allowed: bool
    resulting_state: ArtifactProcessingState | None
    note: str


@dataclass(frozen=True)
class SnapshotTargetPreview:
    allowed: bool
    current_state: ArtifactProcessingState | None
    resulting_state: ArtifactProcessingState | None
    note: str
    row_label: str | None = None


@dataclass(frozen=True)
class ValidationIssue:
    target: str
    reason: str


def add_target_to_pending_batch(batch: PendingBatch | None, target: AdminTarget) -> PendingBatch:
    if batch is None:
        raise ValueError("Cannot add a target without an existing batch")
    if target in batch.targets:
        return batch
    return replace(batch, targets=(*batch.targets, target))


def with_pending_batch_reason(batch: PendingBatch, reason: str) -> PendingBatch:
    return replace(batch, reason=reason)


def bind_pending_batch(batch: PendingBatch, snapshot_info: SnapshotInfo) -> ApplyBatchRequest:
    reason = batch.reason.strip()
    if not reason:
        raise ValueError("A reason is required before applying a batch")

    return ApplyBatchRequest(
        store=batch.store,
        action=batch.action,
        snapshot_id=snapshot_info.snapshot_id,
        base_generation=snapshot_generation_for_store(snapshot_info, batch.store),
        targets=batch.targets,
        reason=reason,
    )


def preview_action(
    store: AdminStore,
    action: AdminActionKind,
    processing_state: ArtifactProcessingState,
    has_required_path: bool,
) -> ActionPreview:
    if processing_state in _excluded_states(store):
        return ActionPreview(False, None, f"state {processing_state.value} is excluded from curated reruns")

    if action == AdminActionKind.QUEUE_EXTRACT:
        if not has_required_path:
            path_label = "mirror_path" if store == AdminStore.IPSW else "download_path"
            return ActionPreview(False, None, f"{path_label} is required to queue extract")
        resulting_state = ArtifactProcessingState.MIRRORED
    else:
        resulting_state = ArtifactProcessingState.INDEXED

    if processing_state == resulting_state:
        return ActionPreview(True, resulting_state, "already eligible; last_run will be refreshed")
    return ActionPreview(True, resulting_state, f"will set state to {resulting_state.value}")


def snapshot_generation_for_store(snapshot_info: SnapshotInfo, store: AdminStore) -> int:
    if store == AdminStore.IPSW:
        return snapshot_info.ipsw_generation
    return snapshot_info.ota_generation


def _create_target_preview(
    store: AdminStore,
    action: AdminActionKind,
    processing_state: ArtifactProcessingState,
    required_path: str | None,
    label: str,
) -> SnapshotTargetPreview:
    preview = preview_action(
        store,
        action,
        processing_state,
        has_required_path=required_path is not None,
    )
    return SnapshotTargetPreview(
        allowed=preview.allowed,
        current_state=processing_state,
        resulting_state=preview.resulting_state,
        note=preview.note,
        row_label=label,
    )


def preview_target_against_snapshot(
    store: AdminStore,
    action: AdminActionKind,
    target: AdminTarget,
    ipsw_rows_by_key: Mapping[str, IpswSourceRow],
    ota_rows_by_key: Mapping[str, OtaArtifactRow],
) -> SnapshotTargetPreview:
    if store == AdminStore.IPSW:
        if not isinstance(target, IpswTarget):
            return SnapshotTargetPreview(False, None, None, "unexpected target type for ipsw batch")

        row = ipsw_rows_by_key.get(_ipsw_target_row_key(target))
        if row is None:
            return SnapshotTargetPreview(False, None, None, "missing from current snapshot")
        else:
            return _create_target_preview(
                store,
                action,
                row.processing_state,
                row.mirror_path,
                row.file_name,
            )

    if not isinstance(target, OtaTarget):
        return SnapshotTargetPreview(False, None, None, "unexpected target type for ota batch")

    row = ota_rows_by_key.get(target.ota_key)
    if row is None:
        return SnapshotTargetPreview(False, None, None, "missing from current snapshot")
    else:
        return _create_target_preview(
            store,
            action,
            row.processing_state,
            row.download_path,
            row.artifact_id,
        )


def validate_pending_batch_against_snapshot(
    batch: PendingBatch,
    ipsw_rows_by_key: Mapping[str, IpswSourceRow],
    ota_rows_by_key: Mapping[str, OtaArtifactRow],
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for target in batch.targets:
        preview = preview_target_against_snapshot(
            batch.store,
            batch.action,
            target,
            ipsw_rows_by_key,
            ota_rows_by_key,
        )
        if not preview.allowed or preview.resulting_state is None:
            issues.append(ValidationIssue(target=target_label(target), reason=preview.note))
    return tuple(issues)


def format_validation_issues(issues: tuple[ValidationIssue, ...]) -> str:
    return "; ".join(f"{issue.target}: {issue.reason}" for issue in issues)


def worker_workflow_for_action(store: AdminStore, action: AdminActionKind) -> str:
    if store == AdminStore.IPSW and action == AdminActionKind.QUEUE_EXTRACT:
        return "symx-ipsw-extract.yml"
    if store == AdminStore.IPSW and action == AdminActionKind.QUEUE_MIRROR:
        return "symx-ipsw-mirror.yml"
    if store == AdminStore.OTA and action == AdminActionKind.QUEUE_EXTRACT:
        return "symx-ota-extract.yml"
    return "symx-ota-mirror.yml"


def action_label(action: AdminActionKind) -> str:
    return {
        AdminActionKind.QUEUE_EXTRACT: "Queue extract",
        AdminActionKind.QUEUE_MIRROR: "Queue mirror",
    }[action]


def target_label(target: AdminTarget) -> str:
    if isinstance(target, IpswTarget):
        return f"{target.artifact_key} :: {target.link}"
    return target.ota_key


def batch_summary(batch: PendingBatch | None) -> str:
    if batch is None:
        return "No pending batch. Highlight rows and press 'e' or 'm' to build one."

    lines = [
        f"store: {batch.store.value}",
        f"action: {batch.action.value}",
        f"reason: {batch.reason or '—'}",
        f"targets: {len(batch.targets)}",
    ]
    for target in batch.targets[:8]:
        lines.append(f"  - {target_label(target)}")
    if len(batch.targets) > 8:
        lines.append(f"  … and {len(batch.targets) - 8} more")
    return "\n".join(lines)


def _excluded_states(store: AdminStore) -> frozenset[ArtifactProcessingState]:
    if store == AdminStore.IPSW:
        return frozenset({ArtifactProcessingState.IGNORED})
    return frozenset(
        {
            ArtifactProcessingState.IGNORED,
            ArtifactProcessingState.INDEXED_DUPLICATE,
            ArtifactProcessingState.DELTA_OTA,
            ArtifactProcessingState.RECOVERY_OTA,
            ArtifactProcessingState.UNSUPPORTED_OTA_PAYLOAD,
        }
    )


def _ipsw_target_row_key(target: IpswTarget) -> str:
    return f"{target.artifact_key}::{target.link}"


def _parse_targets(store: AdminStore, raw_targets: list[object]) -> tuple[AdminTarget, ...]:
    targets: list[AdminTarget] = []
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            raise ValueError("Unexpected target payload")
        payload = cast(dict[str, object], raw_target)
        if store == AdminStore.IPSW:
            targets.append(IpswTarget(artifact_key=str(payload["artifact_key"]), link=str(payload["link"])))
        else:
            targets.append(OtaTarget(ota_key=str(payload["ota_key"])))
    return tuple(targets)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Unexpected integer payload")
    return int(value)
