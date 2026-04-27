import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.text import Text

from symx.admin.actions import AdminActionKind, AdminStore, OtaTarget, PendingBatch
from symx.admin.github import GithubRunInfo
from symx.admin.tui import (
    AdminTui,
    BackgroundTaskEntry,
    DownloadQueueItem,
    format_failure_table_title,
    format_ipsw_detail,
    format_ota_detail,
    format_task_detail,
    format_task_status,
    should_auto_sync_on_start,
    summarize_task_detail,
)
from symx.admin.db import IpswFailureRow, OtaFailureRow, SnapshotInfo
from symx.model import ArtifactProcessingState


def test_format_ipsw_detail_contains_key_fields() -> None:
    row = IpswFailureRow(
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

    detail = format_ipsw_detail(row)

    assert "type: IPSW source" in detail
    assert "state: symbol_extraction_failed" in detail
    assert "last_run: #123" in detail
    assert "file_name: test.ipsw" in detail


def test_should_auto_sync_on_start_for_missing_stale_and_fresh_snapshots() -> None:
    now = datetime(2024, 9, 4, 12, 0, 0, tzinfo=UTC)
    stale_snapshot = SnapshotInfo(
        snapshot_id="ipsw-1__ota-2",
        created_at=(now - timedelta(hours=25)).isoformat(),
        workflow_run_id=1,
        workflow_run_url=None,
        ipsw_generation=1,
        ota_generation=2,
    )
    fresh_snapshot = SnapshotInfo(
        snapshot_id="ipsw-3__ota-4",
        created_at=(now - timedelta(hours=23)).isoformat(),
        workflow_run_id=2,
        workflow_run_url=None,
        ipsw_generation=3,
        ota_generation=4,
    )

    assert should_auto_sync_on_start(None, now=now) is True
    assert should_auto_sync_on_start(stale_snapshot, now=now) is True
    assert should_auto_sync_on_start(fresh_snapshot, now=now) is False


def test_task_formatting_shows_short_row_text_and_full_details() -> None:
    task = BackgroundTaskEntry(
        task_id="task-1",
        kind="download",
        status="warning",
        item="ota-id",
        progress_detail="Downloaded 2048MiB",
        result_detail="Downloaded without SHA verification (unsupported hash algorithm SHA-256): /tmp/very/long/path.zip",
    )

    status = format_task_status(task.status)
    detail = format_task_detail(task)

    assert isinstance(status, Text)
    assert status.plain == "! warning"
    assert summarize_task_detail(task, max_length=32).endswith("…")
    assert "type: Background task" in detail
    assert task.progress_detail in detail
    assert task.result_detail is not None
    assert task.result_detail in detail
    assert format_failure_table_title("IPSW failures", 12) == "IPSW failures (12)"


def test_download_worker_idle_check_avoids_clearing_active_queue(tmp_path: Path) -> None:
    app = AdminTui(cache_dir=tmp_path)
    current = threading.current_thread()

    app._download_worker_thread = current
    assert app._finish_download_worker_if_idle() is True
    assert app._download_worker_thread is None

    app._download_queue.put(
        DownloadQueueItem(
            task_id="task-1",
            item_label="test.ipsw",
            row=IpswFailureRow(
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
            ),
        )
    )
    app._download_worker_thread = current
    assert app._finish_download_worker_if_idle() is False
    assert app._download_worker_thread is current
    app._download_queue.get_nowait()
    app._download_queue.task_done()


def test_format_ota_detail_includes_resolved_run_time() -> None:
    row = OtaFailureRow(
        last_run=456,
        processing_state=ArtifactProcessingState.INDEXED_INVALID,
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
    run_info = GithubRunInfo(
        run_id=456,
        started_at="2024-09-03T10:00:00Z",
        updated_at="2024-09-03T12:34:56Z",
        url="https://example.invalid/run/456",
        display_title="Extract OTA symbols",
    )

    detail = format_ota_detail(row, run_info)

    assert "type: OTA artifact" in detail
    assert "last_run: #456" in detail
    assert "last_run_at: 2024-09-03 12:34Z" in detail
    assert "run_title: Extract OTA symbols" in detail


def test_queue_selected_rejects_extract_batch_item_without_snapshot_download_path(tmp_path: Path, monkeypatch) -> None:
    app = AdminTui(cache_dir=tmp_path)
    row = OtaFailureRow(
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
    statuses: list[str] = []

    app._active_table_id = "ota-failures"
    app._selected_ota_row_key = row.ota_key
    app._ota_rows_by_key = {row.ota_key: row}
    app._all_ota_rows_by_key = {row.ota_key: row}
    monkeypatch.setattr(app, "_set_status", lambda message: statuses.append(message))
    monkeypatch.setattr(app, "_refresh_pending_batch", lambda: None)

    app._queue_selected(AdminActionKind.QUEUE_EXTRACT)

    assert app._pending_batch is None
    assert statuses == ["Cannot add ota-key to queue extract batch: download_path is required to queue extract."]


def test_action_apply_batch_rejects_pending_batch_invalid_for_current_snapshot(tmp_path: Path, monkeypatch) -> None:
    app = AdminTui(cache_dir=tmp_path)
    row = OtaFailureRow(
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
    statuses: list[str] = []
    push_calls: list[tuple[object, ...]] = []

    app._current_snapshot_info = SnapshotInfo(
        snapshot_id="ipsw-101__ota-202",
        created_at="2024-09-03T12:00:00+00:00",
        workflow_run_id=1,
        workflow_run_url=None,
        ipsw_generation=101,
        ota_generation=202,
    )
    app._all_ota_rows_by_key = {row.ota_key: row}
    app._pending_batch = PendingBatch(
        store=AdminStore.OTA,
        action=AdminActionKind.QUEUE_EXTRACT,
        targets=(OtaTarget(ota_key=row.ota_key),),
        reason="",
    )
    monkeypatch.setattr(app, "_set_status", lambda message: statuses.append(message))
    monkeypatch.setattr(app, "push_screen", lambda *args, **kwargs: push_calls.append(args))

    app.action_apply_batch()

    assert push_calls == []
    assert statuses == ["Cannot apply pending batch: ota-key: download_path is required to queue extract"]
