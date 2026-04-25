from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TypedDict, cast

from symx.admin.db import (
    SnapshotManifest,
    SnapshotInfo,
    SnapshotPaths,
    active_snapshot_paths,
    build_snapshot_db,
    load_snapshot_info,
    make_snapshot_id,
    prepare_snapshot_dir,
    snapshot_paths,
    write_manifest,
)
from symx.ipsw.model import IpswArtifactDb
from symx.ota.model import OtaArtifact

ADMIN_META_WORKFLOW = "symx-admin-meta-sync.yml"
ADMIN_META_ARTIFACT = "symx-admin-meta"
ADMIN_META_SUMMARY = "admin_meta_summary.json"


class AdminSyncError(RuntimeError):
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


@dataclass(frozen=True)
class WorkflowArtifactSummary:
    ipsw_generation: int
    ota_generation: int
    ipsw_changed: bool
    ota_changed: bool


@dataclass(frozen=True)
class DownloadedMetaBundle:
    ipsw_db: IpswArtifactDb
    ipsw_generation: int
    ota_meta: dict[str, OtaArtifact]
    ota_generation: int
    ipsw_meta_path: Path
    ota_meta_path: Path
    ipsw_blob_path: Path
    ota_blob_path: Path


@dataclass(frozen=True)
class SyncResult:
    snapshot_id: str
    workflow_run_id: int
    workflow_run_url: str
    ipsw_generation: int
    ota_generation: int
    is_new_snapshot: bool


StatusCallback = Callable[[str], None]


def run_sync(cache_dir: Path, status_callback: StatusCallback | None = None) -> SyncResult:
    latest_paths, latest_info = _latest_cached_snapshot(cache_dir)

    _status(status_callback, f"Checking existing workflow runs for {ADMIN_META_WORKFLOW}…")
    before_max_run_id = _max_run_id(_list_workflow_runs(ADMIN_META_WORKFLOW, limit=10))

    _status(status_callback, f"Dispatching {ADMIN_META_WORKFLOW}…")
    _run_gh_command(_workflow_dispatch_args(latest_info))

    _status(status_callback, "Waiting for the new workflow run to appear…")
    run = _wait_for_new_run(ADMIN_META_WORKFLOW, before_max_run_id)
    run_id = int(run["databaseId"])
    run_url = str(run["url"])
    _status(status_callback, f"Workflow run started: #{run_id}")

    completed_run = _wait_for_run_completion(ADMIN_META_WORKFLOW, run_id, status_callback)
    conclusion = completed_run.get("conclusion")
    if conclusion != "success":
        raise AdminSyncError(f"Admin sync workflow failed ({conclusion or 'unknown'}): {run_url}")

    if not _run_has_artifact(run_id, ADMIN_META_ARTIFACT):
        if latest_info is None:
            raise AdminSyncError("Workflow reported no changes, but there is no local snapshot to keep using")
        _status(status_callback, "Remote meta-data generations are unchanged. No download was needed.")
        write_manifest(cache_dir, SnapshotManifest(active_snapshot_id=latest_info.snapshot_id))
        return SyncResult(
            snapshot_id=latest_info.snapshot_id,
            workflow_run_id=run_id,
            workflow_run_url=run_url,
            ipsw_generation=latest_info.ipsw_generation,
            ota_generation=latest_info.ota_generation,
            is_new_snapshot=False,
        )

    with TemporaryDirectory(prefix="symx_admin_meta_") as temp_dir:
        download_dir = Path(temp_dir)
        _status(status_callback, f"Downloading workflow artifact to {download_dir}…")
        _run_gh_command(["run", "download", str(run_id), "--name", ADMIN_META_ARTIFACT, "--dir", str(download_dir)])

        _status(status_callback, "Parsing downloaded meta-data…")
        bundle = _load_downloaded_bundle(download_dir, latest_paths)
        snapshot_id = make_snapshot_id(bundle.ipsw_generation, bundle.ota_generation)
        paths = snapshot_paths(cache_dir, snapshot_id)
        is_new_snapshot = not _snapshot_is_ready(paths)

        if is_new_snapshot:
            _status(status_callback, f"Building snapshot {snapshot_id}…")
            prepare_snapshot_dir(paths)
            try:
                shutil.copy2(bundle.ipsw_meta_path, paths.ipsw_meta_path)
                shutil.copy2(bundle.ota_meta_path, paths.ota_meta_path)
                shutil.copy2(bundle.ipsw_blob_path, paths.ipsw_blob_path)
                shutil.copy2(bundle.ota_blob_path, paths.ota_blob_path)
                build_snapshot_db(
                    paths.db_path,
                    snapshot_id,
                    bundle.ipsw_db,
                    bundle.ipsw_generation,
                    bundle.ota_meta,
                    bundle.ota_generation,
                    workflow_run_id=run_id,
                    workflow_run_url=run_url,
                )
            except Exception:
                shutil.rmtree(paths.root, ignore_errors=True)
                raise
        else:
            _status(status_callback, f"Snapshot {snapshot_id} already exists locally.")

    write_manifest(cache_dir, SnapshotManifest(active_snapshot_id=snapshot_id))
    _status(status_callback, f"Latest snapshot is now {snapshot_id}.")

    return SyncResult(
        snapshot_id=snapshot_id,
        workflow_run_id=run_id,
        workflow_run_url=run_url,
        ipsw_generation=bundle.ipsw_generation,
        ota_generation=bundle.ota_generation,
        is_new_snapshot=is_new_snapshot,
    )


def _workflow_dispatch_args(latest_info: SnapshotInfo | None) -> list[str]:
    args = ["workflow", "run", ADMIN_META_WORKFLOW]
    if latest_info is None:
        return args

    args.extend(
        [
            "-f",
            f"known_ipsw_generation={latest_info.ipsw_generation}",
            "-f",
            f"known_ota_generation={latest_info.ota_generation}",
        ]
    )
    return args


def _latest_cached_snapshot(cache_dir: Path) -> tuple[SnapshotPaths | None, SnapshotInfo | None]:
    latest_paths = active_snapshot_paths(cache_dir)
    if latest_paths is None:
        return None, None
    return latest_paths, load_snapshot_info(latest_paths.db_path)


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

    raise AdminSyncError(f"Timed out waiting for a new workflow run for {workflow}")


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

    raise AdminSyncError(f"Timed out waiting for workflow run #{run_id} to complete")


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
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise AdminSyncError(f"Unexpected workflow run payload for {workflow}")
    return cast(list[WorkflowRun], data)


def _max_run_id(runs: list[WorkflowRun]) -> int:
    if not runs:
        return 0
    return max(int(run["databaseId"]) for run in runs)


def _run_has_artifact(run_id: int, artifact_name: str) -> bool:
    result = _run_gh_command(["api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/artifacts"])
    raw_payload: object = json.loads(result.stdout)
    if not isinstance(raw_payload, dict):
        raise AdminSyncError(f"Unexpected artifact payload for run #{run_id}")

    payload = cast(WorkflowArtifactList, raw_payload)
    return any(artifact.get("name") == artifact_name for artifact in payload.get("artifacts", []))


def _run_gh_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise AdminSyncError(f"gh command failed: {' '.join(cmd)}\n{stderr}")
    return result


def _load_downloaded_bundle(download_dir: Path, latest_paths: SnapshotPaths | None) -> DownloadedMetaBundle:
    summary = _load_summary(download_dir)
    ipsw_blob_path = _find_single_file(download_dir, "ipsw_meta_blob.json")
    ota_blob_path = _find_single_file(download_dir, "ota_image_meta_blob.json")

    if summary.ipsw_changed:
        ipsw_meta_path = _find_single_file(download_dir, "ipsw_meta.json")
    else:
        if latest_paths is None:
            raise AdminSyncError("Workflow returned only OTA changes, but there is no cached IPSW snapshot")
        ipsw_meta_path = latest_paths.ipsw_meta_path

    if summary.ota_changed:
        ota_meta_path = _find_single_file(download_dir, "ota_image_meta.json")
    else:
        if latest_paths is None:
            raise AdminSyncError("Workflow returned only IPSW changes, but there is no cached OTA snapshot")
        ota_meta_path = latest_paths.ota_meta_path

    return DownloadedMetaBundle(
        ipsw_db=_load_ipsw_db(ipsw_meta_path),
        ipsw_generation=summary.ipsw_generation,
        ota_meta=_load_ota_meta(ota_meta_path),
        ota_generation=summary.ota_generation,
        ipsw_meta_path=ipsw_meta_path,
        ota_meta_path=ota_meta_path,
        ipsw_blob_path=ipsw_blob_path,
        ota_blob_path=ota_blob_path,
    )


def _load_summary(download_dir: Path) -> WorkflowArtifactSummary:
    summary_path = _find_single_file(download_dir, ADMIN_META_SUMMARY)
    raw_payload: object = json.loads(summary_path.read_text())
    if not isinstance(raw_payload, dict):
        raise AdminSyncError("Unexpected admin meta summary payload")

    payload = cast(dict[object, object], raw_payload)
    return WorkflowArtifactSummary(
        ipsw_generation=_coerce_int(payload.get("ipsw_generation"), summary_path, "ipsw_generation"),
        ota_generation=_coerce_int(payload.get("ota_generation"), summary_path, "ota_generation"),
        ipsw_changed=_coerce_bool(payload.get("ipsw_changed"), summary_path, "ipsw_changed"),
        ota_changed=_coerce_bool(payload.get("ota_changed"), summary_path, "ota_changed"),
    )


def _load_ipsw_db(ipsw_meta_path: Path) -> IpswArtifactDb:
    return IpswArtifactDb.model_validate_json(ipsw_meta_path.read_text())


def _load_ota_meta(ota_meta_path: Path) -> dict[str, OtaArtifact]:
    raw_ota_payload: object = json.loads(ota_meta_path.read_text())
    if not isinstance(raw_ota_payload, dict):
        raise AdminSyncError("Unexpected OTA meta-data payload")

    ota_payload = cast(dict[object, object], raw_ota_payload)
    ota_meta: dict[str, OtaArtifact] = {}
    for key, value in ota_payload.items():
        if not isinstance(key, str):
            raise AdminSyncError("Unexpected OTA meta-data key type")
        if not isinstance(value, dict):
            raise AdminSyncError("Unexpected OTA meta-data value type")
        ota_meta[key] = OtaArtifact(**cast(dict[str, Any], value))
    return ota_meta


def _snapshot_is_ready(paths: SnapshotPaths) -> bool:
    required_paths = (
        paths.db_path,
        paths.ipsw_meta_path,
        paths.ota_meta_path,
        paths.ipsw_blob_path,
        paths.ota_blob_path,
    )
    if not all(path.exists() for path in required_paths):
        return False
    return load_snapshot_info(paths.db_path) is not None


def _find_single_file(root: Path, file_name: str) -> Path:
    matches = list(root.rglob(file_name))
    if len(matches) != 1:
        raise AdminSyncError(f"Expected exactly one {file_name} in {root}, found {len(matches)}")
    return matches[0]


def _coerce_int(value: object, source: Path, field_name: str) -> int:
    if not isinstance(value, (int, str)):
        raise AdminSyncError(f"Unexpected {field_name} type in {source}")
    return int(value)


def _coerce_bool(value: object, source: Path, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise AdminSyncError(f"Unexpected {field_name} type in {source}")
    return value
