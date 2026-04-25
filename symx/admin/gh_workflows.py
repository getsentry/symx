from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast


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
ErrorFactory = Callable[[str], Exception]
RunGhCommand = Callable[[list[str]], subprocess.CompletedProcess[str]]
ListWorkflowRuns = Callable[[str, int], list[WorkflowRun]]


def emit_status(status_callback: StatusCallback | None, message: str) -> None:
    if status_callback is not None:
        status_callback(message)


def wait_for_new_run(
    workflow: str,
    before_max_run_id: int,
    error_factory: ErrorFactory,
    list_workflow_runs_func: ListWorkflowRuns | None = None,
) -> WorkflowRun:
    deadline = time.monotonic() + 120.0
    list_runs = list_workflow_runs_func or _list_workflow_runs_for_error(error_factory)
    while time.monotonic() < deadline:
        runs = list_runs(workflow, 10)
        new_runs = [run for run in runs if int(run["databaseId"]) > before_max_run_id]
        if new_runs:
            return max(new_runs, key=lambda run: int(run["databaseId"]))
        time.sleep(2.0)

    raise error_factory(f"Timed out waiting for a new workflow run for {workflow}")


def wait_for_run_completion(
    workflow: str,
    run_id: int,
    status_callback: StatusCallback | None,
    error_factory: ErrorFactory,
    list_workflow_runs_func: ListWorkflowRuns | None = None,
) -> WorkflowRun:
    deadline = time.monotonic() + 3600.0
    last_status: tuple[str, str | None] | None = None
    list_runs = list_workflow_runs_func or _list_workflow_runs_for_error(error_factory)

    while time.monotonic() < deadline:
        runs = list_runs(workflow, 20)
        matching_run = next((run for run in runs if int(run["databaseId"]) == run_id), None)
        if matching_run is None:
            time.sleep(5.0)
            continue

        status = str(matching_run["status"])
        conclusion = matching_run["conclusion"]
        current_status = (status, conclusion)
        if current_status != last_status:
            if status == "completed":
                emit_status(status_callback, f"Workflow #{run_id} completed with conclusion={conclusion}")
            else:
                emit_status(status_callback, f"Workflow #{run_id} status={status}")
            last_status = current_status

        if status == "completed":
            return matching_run

        time.sleep(5.0)

    raise error_factory(f"Timed out waiting for workflow run #{run_id} to complete")


def list_workflow_runs(
    workflow: str,
    limit: int,
    error_factory: ErrorFactory,
    run_gh_command_func: RunGhCommand | None = None,
) -> list[WorkflowRun]:
    run_command = run_gh_command_func or _run_gh_command_for_error(error_factory)
    result = run_command(
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
        raise error_factory(f"Unexpected workflow run payload for {workflow}")
    return cast(list[WorkflowRun], raw_payload)


def max_run_id(runs: list[WorkflowRun]) -> int:
    if not runs:
        return 0
    return max(int(run["databaseId"]) for run in runs)


def run_has_artifact(
    run_id: int,
    artifact_name: str,
    error_factory: ErrorFactory,
    run_gh_command_func: RunGhCommand | None = None,
) -> bool:
    run_command = run_gh_command_func or _run_gh_command_for_error(error_factory)
    result = run_command(["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/artifacts"])
    raw_payload: object = json.loads(result.stdout)
    if not isinstance(raw_payload, dict):
        raise error_factory(f"Unexpected artifact payload for run #{run_id}")

    payload = cast(WorkflowArtifactList, raw_payload)
    return any(artifact.get("name") == artifact_name for artifact in payload.get("artifacts", []))


def run_gh_command(args: list[str], error_factory: ErrorFactory) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise error_factory(f"gh command failed: {' '.join(cmd)}\n{stderr}")
    return result


def find_single_file(root: Path, file_name: str, error_factory: ErrorFactory) -> Path:
    matches = list(root.rglob(file_name))
    if len(matches) != 1:
        raise error_factory(f"Expected exactly one {file_name} in {root}, found {len(matches)}")
    return matches[0]


def _list_workflow_runs_for_error(error_factory: ErrorFactory) -> ListWorkflowRuns:
    def _inner(workflow_name: str, limit: int) -> list[WorkflowRun]:
        return list_workflow_runs(workflow_name, limit, error_factory)

    return _inner


def _run_gh_command_for_error(error_factory: ErrorFactory) -> RunGhCommand:
    def _inner(args: list[str]) -> subprocess.CompletedProcess[str]:
        return run_gh_command(args, error_factory)

    return _inner
