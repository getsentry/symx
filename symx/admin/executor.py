from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict, cast

from symx.admin.actions import ApplyBatchRequest, ApplyBatchResult

ADMIN_APPLY_WORKFLOW = "symx-admin-apply.yml"
ADMIN_APPLY_ARTIFACT = "symx-admin-apply-result"
ADMIN_APPLY_RESULT_FILE = "symx_admin_apply_result.json"


class AdminApplyError(RuntimeError):
    pass


class WorkflowRun(TypedDict):
    databaseId: int
    status: str
    conclusion: str | None
    url: str
    startedAt: str | None
    updatedAt: str | None
    displayTitle: str
    event: str


class WorkflowArtifact(TypedDict, total=False):
    name: str


class WorkflowArtifactList(TypedDict):
    artifacts: list[WorkflowArtifact]


StatusCallback = Callable[[str], None]


def run_apply(request: ApplyBatchRequest, status_callback: StatusCallback | None = None) -> ApplyBatchResult:
    _status(status_callback, f"Checking existing workflow runs for {ADMIN_APPLY_WORKFLOW}…")
    before_max_run_id = _max_run_id(_list_workflow_runs(ADMIN_APPLY_WORKFLOW, limit=10))

    _status(status_callback, f"Dispatching {ADMIN_APPLY_WORKFLOW}…")
    _run_gh_command(_workflow_dispatch_args(request))

    _status(status_callback, "Waiting for the new admin apply workflow run to appear…")
    run = _wait_for_new_run(ADMIN_APPLY_WORKFLOW, before_max_run_id)
    run_id = int(run["databaseId"])
    run_url = str(run["url"])
    _status(status_callback, f"Admin apply workflow run started: #{run_id}")

    completed_run = _wait_for_run_completion(ADMIN_APPLY_WORKFLOW, run_id, status_callback)
    conclusion = completed_run.get("conclusion")

    if not _run_has_artifact(run_id, ADMIN_APPLY_ARTIFACT):
        raise AdminApplyError(
            f"Admin apply workflow finished without a result artifact ({conclusion or 'unknown'}): {run_url}"
        )

    with TemporaryDirectory(prefix="symx_admin_apply_") as temp_dir:
        download_dir = Path(temp_dir)
        _status(status_callback, f"Downloading admin apply artifact to {download_dir}…")
        _run_gh_command(["run", "download", str(run_id), "--name", ADMIN_APPLY_ARTIFACT, "--dir", str(download_dir)])
        result = _load_apply_result(download_dir)

    if conclusion not in {"success", "failure"}:
        raise AdminApplyError(f"Admin apply workflow ended unexpectedly ({conclusion or 'unknown'}): {run_url}")

    return result


def _workflow_dispatch_args(request: ApplyBatchRequest) -> list[str]:
    return [
        "workflow",
        "run",
        ADMIN_APPLY_WORKFLOW,
        "-f",
        f"store={request.store.value}",
        "-f",
        f"action={request.action.value}",
        "-f",
        f"snapshot_id={request.snapshot_id}",
        "-f",
        f"base_generation={request.base_generation}",
        "-f",
        f"reason={request.reason}",
        "-f",
        f"request_json={request.to_json()}",
    ]


def _status(status_callback: StatusCallback | None, message: str) -> None:
    if status_callback is not None:
        status_callback(message)


def _wait_for_new_run(workflow: str, before_max_run_id: int) -> WorkflowRun:
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        runs = _list_workflow_runs(workflow, limit=10)
        new_runs = [run for run in runs if int(run["databaseId"]) > before_max_run_id]
        if new_runs:
            return max(new_runs, key=lambda run: int(run["databaseId"]))
        time.sleep(2.0)

    raise AdminApplyError(f"Timed out waiting for a new workflow run for {workflow}")


def _wait_for_run_completion(
    workflow: str,
    run_id: int,
    status_callback: StatusCallback | None,
) -> WorkflowRun:
    deadline = time.monotonic() + 3600.0
    last_status: tuple[str, str | None] | None = None

    while time.monotonic() < deadline:
        runs = _list_workflow_runs(workflow, limit=20)
        matching_run = next((run for run in runs if int(run["databaseId"]) == run_id), None)
        if matching_run is None:
            time.sleep(5.0)
            continue

        status = str(matching_run["status"])
        conclusion = matching_run["conclusion"]
        current_status = (status, conclusion)
        if current_status != last_status:
            if status == "completed":
                _status(status_callback, f"Workflow #{run_id} completed with conclusion={conclusion}")
            else:
                _status(status_callback, f"Workflow #{run_id} status={status}")
            last_status = current_status

        if status == "completed":
            return matching_run

        time.sleep(5.0)

    raise AdminApplyError(f"Timed out waiting for workflow run #{run_id} to complete")


def _list_workflow_runs(workflow: str, limit: int) -> list[WorkflowRun]:
    result = _run_gh_command(
        [
            "run",
            "list",
            "--workflow",
            workflow,
            "--limit",
            str(limit),
            "--json",
            "databaseId,status,conclusion,url,startedAt,updatedAt,displayTitle,event",
        ]
    )
    raw_payload: object = json.loads(result.stdout)
    if not isinstance(raw_payload, list):
        raise AdminApplyError(f"Unexpected workflow run payload for {workflow}")
    return cast(list[WorkflowRun], raw_payload)


def _max_run_id(runs: list[WorkflowRun]) -> int:
    if not runs:
        return 0
    return max(int(run["databaseId"]) for run in runs)


def _run_has_artifact(run_id: int, artifact_name: str) -> bool:
    result = _run_gh_command(["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/artifacts"])
    raw_payload: object = json.loads(result.stdout)
    if not isinstance(raw_payload, dict):
        raise AdminApplyError(f"Unexpected artifact payload for run #{run_id}")

    payload = cast(WorkflowArtifactList, raw_payload)
    return any(artifact.get("name") == artifact_name for artifact in payload.get("artifacts", []))


def _run_gh_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise AdminApplyError(f"gh command failed: {' '.join(cmd)}\n{stderr}")
    return result


def _load_apply_result(download_dir: Path) -> ApplyBatchResult:
    result_path = _find_single_file(download_dir, ADMIN_APPLY_RESULT_FILE)
    return ApplyBatchResult.from_json(result_path.read_text())


def _find_single_file(root: Path, file_name: str) -> Path:
    matches = list(root.rglob(file_name))
    if len(matches) != 1:
        raise AdminApplyError(f"Expected exactly one {file_name} in {root}, found {len(matches)}")
    return matches[0]
