from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket, Client

from symx.admin.actions import (
    AdminStore,
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyBatchStatus,
    WorkerDispatchResult,
    WorkerDispatchStatus,
    worker_workflow_for_action,
)
from symx.admin.apply import AdminApplyValidationError, apply_request_to_ipsw_db, apply_request_to_ota_meta
from symx.gcs import parse_gcs_url
from symx.ipsw.model import ARTIFACTS_META_JSON as IPSW_META_JSON, IpswArtifactDb
from symx.ota.model import ARTIFACTS_META_JSON as OTA_META_JSON, OtaArtifact

StatusCallback = Callable[[str], None]


class AdminRemoteApplyError(RuntimeError):
    pass


def execute_apply_request(
    storage: str,
    request: ApplyBatchRequest,
    status_callback: StatusCallback | None = None,
) -> ApplyBatchResult:
    try:
        bucket = _bucket_for_storage(storage)
        blob = bucket.blob(_meta_blob_name(request.store))
        remote_generation = _blob_generation(blob)
        _status(status_callback, f"Remote {request.store.value} generation is {remote_generation}.")
        if remote_generation != request.base_generation:
            return _result(
                request,
                status=ApplyBatchStatus.STALE_GENERATION,
                remote_generation=remote_generation,
                applied_count=0,
                message=(
                    f"Refusing to apply against stale generation: local={request.base_generation}, remote={remote_generation}"
                ),
            )

        if request.store == AdminStore.IPSW:
            ipsw_db = _load_ipsw_db(blob)
            applied_count = apply_request_to_ipsw_db(ipsw_db, request)
            serialized_payload = ipsw_db.model_dump_json()
        else:
            ota_meta = _load_ota_meta(blob)
            applied_count = apply_request_to_ota_meta(ota_meta, request)
            serialized_payload = json.dumps({key: value.model_dump() for key, value in ota_meta.items()})
    except AdminApplyValidationError as exc:
        return _result(
            request,
            status=ApplyBatchStatus.VALIDATION_FAILED,
            remote_generation=request.base_generation,
            applied_count=0,
            message=str(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive guard around remote execution
        return _result(
            request,
            status=ApplyBatchStatus.INTERNAL_ERROR,
            remote_generation=request.base_generation,
            applied_count=0,
            message=str(exc),
        )

    _status(
        status_callback,
        f"Uploading updated {request.store.value} meta-data with generation match {request.base_generation}…",
    )
    try:
        blob.upload_from_string(serialized_payload, if_generation_match=request.base_generation)
    except PreconditionFailed:
        current_generation = _blob_generation(blob)
        return _result(
            request,
            status=ApplyBatchStatus.STALE_GENERATION,
            remote_generation=current_generation,
            applied_count=0,
            message=(
                f"Remote generation changed during apply: local={request.base_generation}, remote={current_generation}"
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive guard around remote execution
        return _result(
            request,
            status=ApplyBatchStatus.INTERNAL_ERROR,
            remote_generation=remote_generation,
            applied_count=0,
            message=str(exc),
        )

    worker = ensure_worker_running(request, status_callback=status_callback)
    status = ApplyBatchStatus.APPLIED
    message = f"Applied {request.action.value} to {applied_count} {request.store.value} rows."
    if worker.status == WorkerDispatchStatus.DISPATCH_FAILED:
        status = ApplyBatchStatus.APPLIED_WITH_WORKER_WARNING
        message = f"{message} Worker dispatch warning: {worker.detail or 'unknown gh error'}"

    return _result(
        request,
        status=status,
        remote_generation=remote_generation,
        applied_count=applied_count,
        message=message,
        worker=worker,
    )


def ensure_worker_running(
    request: ApplyBatchRequest,
    status_callback: StatusCallback | None = None,
) -> WorkerDispatchResult:
    workflow = worker_workflow_for_action(request.store, request.action)
    _status(status_callback, f"Checking whether {workflow} is already running…")

    try:
        runs = _list_workflow_runs(workflow)
    except AdminRemoteApplyError as exc:
        return WorkerDispatchResult(workflow=workflow, status=WorkerDispatchStatus.DISPATCH_FAILED, detail=str(exc))

    active_run = next((run for run in runs if run.get("status") != "completed"), None)
    if active_run is not None:
        detail = f"already running: {active_run.get('url') or active_run.get('databaseId') or workflow}"
        _status(status_callback, f"{workflow} is already running.")
        return WorkerDispatchResult(workflow=workflow, status=WorkerDispatchStatus.ALREADY_RUNNING, detail=detail)

    _status(status_callback, f"Dispatching {workflow}…")
    try:
        _run_gh_command(["workflow", "run", workflow])
    except AdminRemoteApplyError as exc:
        return WorkerDispatchResult(workflow=workflow, status=WorkerDispatchStatus.DISPATCH_FAILED, detail=str(exc))

    return WorkerDispatchResult(workflow=workflow, status=WorkerDispatchStatus.DISPATCHED, detail="dispatched")


def write_apply_result(result_path: Path, result: ApplyBatchResult) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(result.to_json() + "\n")


def append_apply_summary(summary_path: Path | None, result: ApplyBatchResult) -> None:
    if summary_path is None:
        return
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Admin apply: {result.status.value}",
        "",
        f"- store: `{result.store.value}`",
        f"- action: `{result.action.value}`",
        f"- snapshot: `{result.snapshot_id}`",
        f"- base generation: `{result.base_generation}`",
        f"- remote generation: `{result.remote_generation}`",
        f"- applied count: `{result.applied_count}`",
        f"- reason: {result.reason}",
        f"- message: {result.message}",
    ]
    if result.worker is not None:
        lines.extend(
            [
                f"- worker workflow: `{result.worker.workflow}`",
                f"- worker status: `{result.worker.status.value}`",
                f"- worker detail: {result.worker.detail or '—'}",
            ]
        )
    summary_path.write_text("\n".join(lines) + "\n")


def _bucket_for_storage(storage: str) -> Bucket:
    uri = parse_gcs_url(storage)
    if uri is None or uri.hostname is None:
        raise AdminRemoteApplyError("Unsupported or invalid GCS storage URI")
    client = Client(project=uri.username)
    return client.bucket(uri.hostname)


def _meta_blob_name(store: AdminStore) -> str:
    if store == AdminStore.IPSW:
        return IPSW_META_JSON
    return OTA_META_JSON


def _blob_generation(blob: Blob) -> int:
    blob.reload()
    generation = blob.generation
    if generation is None:
        return 0
    return int(generation)


def _load_ipsw_db(blob: Blob) -> IpswArtifactDb:
    payload = str(cast(Any, blob).download_as_text())
    return IpswArtifactDb.model_validate_json(payload)


def _load_ota_meta(blob: Blob) -> dict[str, OtaArtifact]:
    payload = str(cast(Any, blob).download_as_text())
    raw_payload: object = json.loads(payload)
    if not isinstance(raw_payload, dict):
        raise AdminRemoteApplyError("Unexpected OTA meta-data payload")

    payload = cast(dict[object, object], raw_payload)
    result: dict[str, OtaArtifact] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise AdminRemoteApplyError("Unexpected OTA meta-data key type")
        if not isinstance(value, dict):
            raise AdminRemoteApplyError("Unexpected OTA meta-data value type")
        result[key] = OtaArtifact(**cast(dict[str, Any], value))
    return result


def _list_workflow_runs(workflow: str, limit: int = 10) -> list[dict[str, object]]:
    result = _run_gh_command(
        [
            "run",
            "list",
            "--workflow",
            workflow,
            "--limit",
            str(limit),
            "--json",
            "databaseId,status,url",
        ]
    )
    raw_payload: object = json.loads(result.stdout)
    if not isinstance(raw_payload, list):
        raise AdminRemoteApplyError(f"Unexpected workflow run payload for {workflow}")
    return cast(list[dict[str, object]], raw_payload)


def _run_gh_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise AdminRemoteApplyError(f"gh command failed: {' '.join(cmd)}\n{stderr}")
    return result


def _result(
    request: ApplyBatchRequest,
    *,
    status: ApplyBatchStatus,
    remote_generation: int,
    applied_count: int,
    message: str,
    worker: WorkerDispatchResult | None = None,
) -> ApplyBatchResult:
    return ApplyBatchResult(
        status=status,
        store=request.store,
        action=request.action,
        snapshot_id=request.snapshot_id,
        base_generation=request.base_generation,
        remote_generation=remote_generation,
        targets=request.targets,
        reason=request.reason,
        applied_count=applied_count,
        message=message,
        worker=worker,
    )


def _status(status_callback: StatusCallback | None, message: str) -> None:
    if status_callback is not None:
        status_callback(message)
