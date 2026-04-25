from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from symx.admin import gh_workflows
from symx.admin.actions import ApplyBatchRequest, ApplyBatchResult
from symx.admin.gh_workflows import StatusCallback, WorkflowRun

ADMIN_APPLY_WORKFLOW = "symx-admin-apply.yml"
ADMIN_APPLY_ARTIFACT = "symx-admin-apply-result"
ADMIN_APPLY_RESULT_FILE = "symx_admin_apply_result.json"


class AdminApplyError(RuntimeError):
    pass


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
    gh_workflows.emit_status(status_callback, message)


def _wait_for_new_run(workflow: str, before_max_run_id: int) -> WorkflowRun:
    return gh_workflows.wait_for_new_run(workflow, before_max_run_id, AdminApplyError, _list_workflow_runs)


def _wait_for_run_completion(
    workflow: str,
    run_id: int,
    status_callback: StatusCallback | None,
) -> WorkflowRun:
    return gh_workflows.wait_for_run_completion(workflow, run_id, status_callback, AdminApplyError, _list_workflow_runs)


def _list_workflow_runs(workflow: str, limit: int) -> list[WorkflowRun]:
    return gh_workflows.list_workflow_runs(workflow, limit, AdminApplyError, _run_gh_command)


def _max_run_id(runs: list[WorkflowRun]) -> int:
    return gh_workflows.max_run_id(runs)


def _run_has_artifact(run_id: int, artifact_name: str) -> bool:
    return gh_workflows.run_has_artifact(run_id, artifact_name, AdminApplyError, _run_gh_command)


def _run_gh_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return gh_workflows.run_gh_command(args, AdminApplyError)


def _load_apply_result(download_dir: Path) -> ApplyBatchResult:
    result_path = _find_single_file(download_dir, ADMIN_APPLY_RESULT_FILE)
    return ApplyBatchResult.from_json(result_path.read_text())


def _find_single_file(root: Path, file_name: str) -> Path:
    return gh_workflows.find_single_file(root, file_name, AdminApplyError)
