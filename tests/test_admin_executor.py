import json
import subprocess
from pathlib import Path

from symx.admin.actions import (
    AdminActionKind,
    AdminStore,
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyBatchStatus,
    OtaTarget,
)
from symx.admin.executor import ADMIN_APPLY_ARTIFACT, ADMIN_APPLY_RESULT_FILE, run_apply


class _FakeGh:
    def __init__(self, *, artifacts_by_run: dict[int, bool], result_by_run: dict[int, ApplyBatchResult]) -> None:
        self.artifacts_by_run = artifacts_by_run
        self.result_by_run = result_by_run
        self.dispatches: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["workflow", "run"]:
            self.dispatches.append(args)
            return subprocess.CompletedProcess(["gh", *args], 0, "", "")

        if args[:2] == ["api", "repos/{owner}/{repo}/actions/runs/600/artifacts"]:
            return self._artifact_response(600, args)
        if args[:2] == ["api", "repos/{owner}/{repo}/actions/runs/601/artifacts"]:
            return self._artifact_response(601, args)

        if args[:2] == ["run", "download"]:
            run_id = int(args[2])
            download_dir = Path(args[args.index("--dir") + 1])
            download_dir.mkdir(parents=True, exist_ok=True)
            (download_dir / ADMIN_APPLY_RESULT_FILE).write_text(self.result_by_run[run_id].to_json())
            return subprocess.CompletedProcess(["gh", *args], 0, "", "")

        return subprocess.CompletedProcess(["gh", *args], 0, "", "")

    def _artifact_response(self, run_id: int, args: list[str]) -> subprocess.CompletedProcess[str]:
        artifacts = []
        if self.artifacts_by_run.get(run_id, False):
            artifacts.append({"name": ADMIN_APPLY_ARTIFACT})
        return subprocess.CompletedProcess(["gh", *args], 0, json.dumps({"artifacts": artifacts}), "")


def test_run_apply_returns_structured_stale_generation_result(monkeypatch) -> None:
    request = ApplyBatchRequest(
        store=AdminStore.OTA,
        action=AdminActionKind.QUEUE_MIRROR,
        snapshot_id="ipsw-101__ota-202",
        base_generation=202,
        reason="retry mirror",
        targets=(OtaTarget(ota_key="ota-key"),),
    )
    expected = ApplyBatchResult(
        status=ApplyBatchStatus.STALE_GENERATION,
        store=request.store,
        action=request.action,
        snapshot_id=request.snapshot_id,
        base_generation=request.base_generation,
        remote_generation=303,
        targets=request.targets,
        reason=request.reason,
        applied_count=0,
        message="stale",
        worker=None,
    )
    fake_gh = _FakeGh(artifacts_by_run={600: True}, result_by_run={600: expected})

    monkeypatch.setattr("symx.admin.executor._list_workflow_runs", lambda workflow, limit: [])
    monkeypatch.setattr("symx.admin.executor._run_gh_command", fake_gh)
    monkeypatch.setattr(
        "symx.admin.executor._wait_for_new_run",
        lambda workflow, before_max_run_id: {"databaseId": 600, "url": "https://example.invalid/run/600"},
    )
    monkeypatch.setattr(
        "symx.admin.executor._wait_for_run_completion",
        lambda workflow, run_id, status_callback: {
            "databaseId": run_id,
            "url": f"https://example.invalid/run/{run_id}",
            "status": "completed",
            "conclusion": "failure",
        },
    )

    result = run_apply(request)

    assert result == expected
    assert any(arg == f"request_json={request.to_json()}" for arg in fake_gh.dispatches[0])


def test_run_apply_returns_success_result(monkeypatch) -> None:
    request = ApplyBatchRequest(
        store=AdminStore.OTA,
        action=AdminActionKind.QUEUE_EXTRACT,
        snapshot_id="ipsw-101__ota-202",
        base_generation=202,
        reason="retry extract",
        targets=(OtaTarget(ota_key="ota-key"),),
    )
    expected = ApplyBatchResult(
        status=ApplyBatchStatus.APPLIED,
        store=request.store,
        action=request.action,
        snapshot_id=request.snapshot_id,
        base_generation=request.base_generation,
        remote_generation=202,
        targets=request.targets,
        reason=request.reason,
        applied_count=1,
        message="applied",
        worker=None,
    )
    fake_gh = _FakeGh(artifacts_by_run={601: True}, result_by_run={601: expected})

    monkeypatch.setattr("symx.admin.executor._list_workflow_runs", lambda workflow, limit: [])
    monkeypatch.setattr("symx.admin.executor._run_gh_command", fake_gh)
    monkeypatch.setattr(
        "symx.admin.executor._wait_for_new_run",
        lambda workflow, before_max_run_id: {"databaseId": 601, "url": "https://example.invalid/run/601"},
    )
    monkeypatch.setattr(
        "symx.admin.executor._wait_for_run_completion",
        lambda workflow, run_id, status_callback: {
            "databaseId": run_id,
            "url": f"https://example.invalid/run/{run_id}",
            "status": "completed",
            "conclusion": "success",
        },
    )

    result = run_apply(request)

    assert result == expected
