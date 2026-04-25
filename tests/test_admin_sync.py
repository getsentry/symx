import json
import subprocess
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from pydantic import HttpUrl

import pytest

from symx.admin.db import load_snapshot_info, read_manifest, snapshot_paths
from symx.admin.sync import ADMIN_META_ARTIFACT, ADMIN_META_SUMMARY, AdminSyncError, _coerce_int, run_sync
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


def _sample_ipsw_db() -> IpswArtifactDb:
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
                        processing_state=ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
                        last_run=111,
                        last_modified=datetime(2024, 9, 3, 12, 0, 0),
                    )
                ],
            )
        }
    )


def _sample_ota_meta(last_run: int = 222, artifact_id: str = "ota-id") -> dict[str, OtaArtifact]:
    return {
        "ota-key": OtaArtifact(
            build="22A100",
            description=["full"],
            version="18.0",
            platform="ios",
            id=artifact_id,
            url=f"https://updates.cdn-apple.com/{artifact_id}.zip",
            download_path=None,
            devices=["iPhone17,1"],
            hash="def",
            hash_algorithm="SHA-1",
            last_run=last_run,
            processing_state=ArtifactProcessingState.INDEXED_INVALID,
        )
    }


def _write_admin_meta_artifact(
    download_dir: Path,
    *,
    ipsw_generation: int,
    ota_generation: int,
    ipsw_changed: bool,
    ota_changed: bool,
    ipsw_db: IpswArtifactDb | None = None,
    ota_meta: dict[str, OtaArtifact] | None = None,
) -> None:
    download_dir.mkdir(parents=True, exist_ok=True)
    (download_dir / ADMIN_META_SUMMARY).write_text(
        json.dumps(
            {
                "ipsw_generation": ipsw_generation,
                "ota_generation": ota_generation,
                "ipsw_changed": ipsw_changed,
                "ota_changed": ota_changed,
            }
        )
    )
    (download_dir / "ipsw_meta_blob.json").write_text(json.dumps({"generation": ipsw_generation}))
    (download_dir / "ota_image_meta_blob.json").write_text(json.dumps({"generation": ota_generation}))
    if ipsw_changed:
        assert ipsw_db is not None
        (download_dir / "ipsw_meta.json").write_text(ipsw_db.model_dump_json())
    if ota_changed:
        assert ota_meta is not None
        (download_dir / "ota_image_meta.json").write_text(
            json.dumps({key: value.model_dump() for key, value in ota_meta.items()})
        )


class _FakeGh:
    def __init__(
        self,
        *,
        artifacts_by_run: dict[int, bool],
        downloads_by_run: dict[int, Callable[[Path], None]],
    ) -> None:
        self.artifacts_by_run = artifacts_by_run
        self.downloads_by_run = downloads_by_run
        self.dispatches: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["workflow", "run"]:
            self.dispatches.append(args)
            return subprocess.CompletedProcess(["gh", *args], 0, "", "")

        if args[:2] == ["api", "repos/{owner}/{repo}/actions/runs/555/artifacts"]:
            return self._artifact_response(555, args)
        if args[:2] == ["api", "repos/{owner}/{repo}/actions/runs/556/artifacts"]:
            return self._artifact_response(556, args)
        if args[:2] == ["api", "repos/{owner}/{repo}/actions/runs/557/artifacts"]:
            return self._artifact_response(557, args)

        if args[:2] == ["run", "download"]:
            run_id = int(args[2])
            download_dir = Path(args[args.index("--dir") + 1])
            writer = self.downloads_by_run.get(run_id)
            if writer is None:
                raise AssertionError(f"Unexpected download for run {run_id}")
            writer(download_dir)
            return subprocess.CompletedProcess(["gh", *args], 0, "", "")

        return subprocess.CompletedProcess(["gh", *args], 0, "", "")

    def _artifact_response(self, run_id: int, args: list[str]) -> subprocess.CompletedProcess[str]:
        artifacts = []
        if self.artifacts_by_run.get(run_id, False):
            artifacts.append({"name": ADMIN_META_ARTIFACT})
        return subprocess.CompletedProcess(["gh", *args], 0, json.dumps({"artifacts": artifacts}), "")


def test_run_sync_creates_snapshot_then_short_circuits_when_unchanged(tmp_path: Path, monkeypatch) -> None:
    ipsw_db = _sample_ipsw_db()
    ota_meta = _sample_ota_meta()
    fake_gh = _FakeGh(
        artifacts_by_run={555: True, 556: False},
        downloads_by_run={
            555: lambda download_dir: _write_admin_meta_artifact(
                download_dir,
                ipsw_generation=101,
                ota_generation=202,
                ipsw_changed=True,
                ota_changed=True,
                ipsw_db=ipsw_db,
                ota_meta=ota_meta,
            )
        },
    )

    monkeypatch.setattr("symx.admin.sync._list_workflow_runs", lambda workflow, limit: [])
    monkeypatch.setattr("symx.admin.sync._run_gh_command", fake_gh)
    monkeypatch.setattr(
        "symx.admin.sync._wait_for_new_run",
        lambda workflow, before_max_run_id: {
            "databaseId": 555 if not fake_gh.dispatches or len(fake_gh.dispatches) == 1 else 556,
            "url": "https://example.invalid/run/555",
        },
    )
    monkeypatch.setattr(
        "symx.admin.sync._wait_for_run_completion",
        lambda workflow, run_id, status_callback: {
            "databaseId": run_id,
            "url": f"https://example.invalid/run/{run_id}",
            "status": "completed",
            "conclusion": "success",
        },
    )

    messages: list[str] = []
    first = run_sync(tmp_path, status_callback=messages.append)
    second = run_sync(tmp_path, status_callback=messages.append)

    assert first.snapshot_id == "ipsw-101__ota-202"
    assert first.is_new_snapshot is True
    assert second.snapshot_id == first.snapshot_id
    assert second.is_new_snapshot is False

    manifest = read_manifest(tmp_path)
    assert manifest.active_snapshot_id == first.snapshot_id

    info = load_snapshot_info(snapshot_paths(tmp_path, first.snapshot_id).db_path)
    assert info is not None
    assert info.workflow_run_id == 555
    assert info.ipsw_generation == 101
    assert info.ota_generation == 202
    assert any("No download was needed." in message for message in messages)
    assert fake_gh.dispatches[1] == [
        "workflow",
        "run",
        "symx-admin-meta-sync.yml",
        "-f",
        "known_ipsw_generation=101",
        "-f",
        "known_ota_generation=202",
    ]


def test_run_sync_merges_partial_update_with_latest_local_snapshot(tmp_path: Path, monkeypatch) -> None:
    ipsw_db = _sample_ipsw_db()
    ota_meta_v1 = _sample_ota_meta(last_run=222, artifact_id="ota-id-v1")
    ota_meta_v2 = _sample_ota_meta(last_run=333, artifact_id="ota-id-v2")
    fake_gh = _FakeGh(
        artifacts_by_run={555: True, 556: True},
        downloads_by_run={
            555: lambda download_dir: _write_admin_meta_artifact(
                download_dir,
                ipsw_generation=101,
                ota_generation=202,
                ipsw_changed=True,
                ota_changed=True,
                ipsw_db=ipsw_db,
                ota_meta=ota_meta_v1,
            ),
            556: lambda download_dir: _write_admin_meta_artifact(
                download_dir,
                ipsw_generation=101,
                ota_generation=303,
                ipsw_changed=False,
                ota_changed=True,
                ota_meta=ota_meta_v2,
            ),
        },
    )

    monkeypatch.setattr("symx.admin.sync._list_workflow_runs", lambda workflow, limit: [])
    monkeypatch.setattr("symx.admin.sync._run_gh_command", fake_gh)
    next_run_ids = iter([555, 556])
    monkeypatch.setattr(
        "symx.admin.sync._wait_for_new_run",
        lambda workflow, before_max_run_id: {
            "databaseId": next(next_run_ids),
            "url": "https://example.invalid/run",
        },
    )
    monkeypatch.setattr(
        "symx.admin.sync._wait_for_run_completion",
        lambda workflow, run_id, status_callback: {
            "databaseId": run_id,
            "url": f"https://example.invalid/run/{run_id}",
            "status": "completed",
            "conclusion": "success",
        },
    )

    first = run_sync(tmp_path)
    second = run_sync(tmp_path)

    assert first.snapshot_id == "ipsw-101__ota-202"
    assert second.snapshot_id == "ipsw-101__ota-303"
    assert second.is_new_snapshot is True

    info = load_snapshot_info(snapshot_paths(tmp_path, second.snapshot_id).db_path)
    assert info is not None
    assert info.ipsw_generation == 101
    assert info.ota_generation == 303
    assert fake_gh.dispatches[1] == [
        "workflow",
        "run",
        "symx-admin-meta-sync.yml",
        "-f",
        "known_ipsw_generation=101",
        "-f",
        "known_ota_generation=202",
    ]


def test_sync_coerce_int_rejects_boolean_values(tmp_path: Path) -> None:
    with pytest.raises(AdminSyncError, match="Unexpected ipsw_generation type"):
        _coerce_int(True, tmp_path / "summary.json", "ipsw_generation")


def test_run_sync_rebuilds_incomplete_snapshot(tmp_path: Path, monkeypatch) -> None:
    ipsw_db = _sample_ipsw_db()
    ota_meta = _sample_ota_meta()
    incomplete_paths = snapshot_paths(tmp_path, "ipsw-101__ota-202")
    incomplete_paths.root.mkdir(parents=True, exist_ok=True)
    incomplete_paths.db_path.write_text("")

    fake_gh = _FakeGh(
        artifacts_by_run={557: True},
        downloads_by_run={
            557: lambda download_dir: _write_admin_meta_artifact(
                download_dir,
                ipsw_generation=101,
                ota_generation=202,
                ipsw_changed=True,
                ota_changed=True,
                ipsw_db=ipsw_db,
                ota_meta=ota_meta,
            )
        },
    )

    monkeypatch.setattr("symx.admin.sync._list_workflow_runs", lambda workflow, limit: [])
    monkeypatch.setattr("symx.admin.sync._run_gh_command", fake_gh)
    monkeypatch.setattr(
        "symx.admin.sync._wait_for_new_run",
        lambda workflow, before_max_run_id: {"databaseId": 557, "url": "https://example.invalid/run/557"},
    )
    monkeypatch.setattr(
        "symx.admin.sync._wait_for_run_completion",
        lambda workflow, run_id, status_callback: {
            "databaseId": 557,
            "url": "https://example.invalid/run/557",
            "status": "completed",
            "conclusion": "success",
        },
    )

    result = run_sync(tmp_path)

    assert result.snapshot_id == "ipsw-101__ota-202"
    assert result.is_new_snapshot is True
    info = load_snapshot_info(incomplete_paths.db_path)
    assert info is not None
    assert info.ipsw_generation == 101
