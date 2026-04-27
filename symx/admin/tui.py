from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Queue
from typing import cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, SelectionList, Static

from symx.admin.actions import (
    AdminActionKind,
    AdminStore,
    ApplyBatchResult,
    ApplyBatchStatus,
    IpswTarget,
    OtaTarget,
    PendingBatch,
    action_label,
    add_target_to_pending_batch,
    batch_summary,
    bind_pending_batch,
    format_validation_issues,
    preview_target_against_snapshot,
    target_label,
    validate_pending_batch_against_snapshot,
    with_pending_batch_reason,
)
from symx.admin.db import (
    DEFAULT_FAILURE_STATES,
    IpswSourceRow,
    OtaArtifactRow,
    SnapshotInfo,
    active_snapshot_paths,
    load_ipsw_rows,
    load_ota_rows,
    load_snapshot_info,
    snapshot_paths,
)
from symx.admin.downloads import ArtifactDownloadResult, download_ipsw_to_cache, download_ota_to_cache
from symx.admin.executor import AdminApplyError, run_apply
from symx.admin.github import GithubRunInfo, ensure_github_run_infos, format_github_run_time, format_iso_timestamp
from symx.admin.sync import AdminSyncError, SyncResult, run_sync
from symx.model import ArtifactProcessingState

EMPTY = "—"
AUTO_SYNC_MAX_AGE = timedelta(hours=24)
MAX_TASK_ROWS = 12


class StateFilterScreen(ModalScreen[tuple[ArtifactProcessingState, ...] | None]):
    CSS = """
    StateFilterScreen {
        align: center middle;
        background: $background 70%;
    }

    #state-filter-dialog {
        width: 72;
        height: auto;
        max-height: 28;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #state-filter-buttons {
        layout: horizontal;
        align-horizontal: right;
        padding-top: 1;
        height: auto;
    }

    #state-filter-buttons Button {
        margin-left: 1;
    }

    SelectionList {
        height: 16;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, selected_states: tuple[ArtifactProcessingState, ...]) -> None:
        super().__init__()
        self.selected_states = selected_states

    def compose(self) -> ComposeResult:
        with Vertical(id="state-filter-dialog"):
            yield Static("Choose processing states to show", classes="section-title")
            yield SelectionList[ArtifactProcessingState](
                *[(state.value, state, state in self.selected_states) for state in ArtifactProcessingState],
                id="state-filter-selection",
            )
            with Horizontal(id="state-filter-buttons"):
                yield Button("Apply", id="apply")
                yield Button("Defaults", id="defaults")
                yield Button("Cancel", id="cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "cancel":
            self.dismiss(None)
            return
        if button_id == "defaults":
            self.dismiss(DEFAULT_FAILURE_STATES)
            return
        if button_id == "apply":
            selection_list = cast(
                SelectionList[ArtifactProcessingState], self.query_one("#state-filter-selection", SelectionList)
            )
            self.dismiss(tuple(selection_list.selected))


class ReasonScreen(ModalScreen[str | None]):
    CSS = """
    ReasonScreen {
        align: center middle;
        background: $background 70%;
    }

    #reason-dialog {
        width: 72;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }

    #reason-buttons {
        layout: horizontal;
        align-horizontal: right;
        padding-top: 1;
        height: auto;
    }

    #reason-buttons Button {
        margin-left: 1;
    }

    #reason-input {
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, initial_reason: str = "") -> None:
        super().__init__()
        self.initial_reason = initial_reason

    def compose(self) -> ComposeResult:
        with Vertical(id="reason-dialog"):
            yield Static("Provide a reason for this admin rerun batch", classes="section-title")
            yield Input(value=self.initial_reason, placeholder="Reason", id="reason-input")
            with Horizontal(id="reason-buttons"):
                yield Button("Apply", id="apply")
                yield Button("Cancel", id="cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id == "apply":
            reason = self.query_one("#reason-input", Input).value.strip()
            if reason:
                self.dismiss(reason)


@dataclass(frozen=True)
class BackgroundTaskEntry:
    task_id: str
    kind: str
    status: str
    item: str
    progress_detail: str
    result_detail: str | None = None


@dataclass(frozen=True)
class DownloadQueueItem:
    task_id: str
    item_label: str
    row: IpswSourceRow | OtaArtifactRow


class AdminTui(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #status {
        height: 3;
        padding: 0 1;
        background: $panel;
        color: $text;
    }

    #body {
        height: 1fr;
        layout: horizontal;
    }

    #tables {
        width: 2fr;
        height: 1fr;
        layout: vertical;
    }

    #details-pane {
        width: 1fr;
        min-width: 42;
        height: 1fr;
        border-left: solid $primary;
        layout: vertical;
    }

    #details {
        height: 1fr;
        padding: 0 1;
    }

    #pending-batch {
        height: 12;
        padding: 0 1;
    }

    #tasks {
        height: 12;
    }

    .section-title {
        padding: 0 1;
        text-style: bold;
    }

    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("s", "sync", "Refresh"),
        Binding("u", "use_latest", "Use latest"),
        Binding("f", "edit_filters", "Filters"),
        Binding("d", "download_selected", "Download"),
        Binding("e", "queue_extract_selected", "Queue extract"),
        Binding("m", "queue_mirror_selected", "Queue mirror"),
        Binding("a", "apply_batch", "Apply batch"),
        Binding("c", "clear_batch", "Clear batch"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self, cache_dir: Path, failure_states: tuple[ArtifactProcessingState, ...] = DEFAULT_FAILURE_STATES
    ) -> None:
        super().__init__()
        self.cache_dir = cache_dir
        self.failure_states = failure_states
        self._sync_lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None
        self._sync_activate_latest = False
        self._apply_thread: threading.Thread | None = None
        self._download_worker_lock = threading.Lock()
        self._download_worker_thread: threading.Thread | None = None
        self._run_lookup_thread: threading.Thread | None = None
        self._download_queue: Queue[DownloadQueueItem] = Queue()
        self.current_snapshot_id: str | None = None
        self.pending_snapshot_id: str | None = None
        self._current_snapshot_info: SnapshotInfo | None = None
        self._run_infos: dict[int, GithubRunInfo] = ensure_github_run_infos(cache_dir, [])
        self._ipsw_rows: list[IpswSourceRow] = []
        self._ipsw_rows_by_key: dict[str, IpswSourceRow] = {}
        self._all_ipsw_rows_by_key: dict[str, IpswSourceRow] = {}
        self._selected_ipsw_row_key: str | None = None
        self._ota_rows: list[OtaArtifactRow] = []
        self._ota_rows_by_key: dict[str, OtaArtifactRow] = {}
        self._all_ota_rows_by_key: dict[str, OtaArtifactRow] = {}
        self._selected_ota_row_key: str | None = None
        self._active_table_id: str | None = None
        self._selected_task_id: str | None = None
        self._pending_batch: PendingBatch | None = None
        self._task_counter = 0
        self._task_order: list[str] = []
        self._tasks: dict[str, BackgroundTaskEntry] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._status_text("Opening admin cache…"), id="status")
        with Horizontal(id="body"):
            with Vertical(id="tables"):
                yield Static(format_failure_table_title("IPSW sources", 0), classes="section-title", id="ipsw-title")
                yield DataTable[str](id="ipsw-failures", cursor_type="row")
                yield Static(format_failure_table_title("OTA artifacts", 0), classes="section-title", id="ota-title")
                yield DataTable[str](id="ota-failures", cursor_type="row")
            with Vertical(id="details-pane"):
                yield Static("Details", classes="section-title")
                yield Static(self._detail_text(), id="details")
                yield Static("Pending batch", classes="section-title")
                yield Static(self._pending_batch_text(), id="pending-batch")
                yield Static("Background tasks", classes="section-title")
                yield DataTable[object](id="tasks", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "symx admin"
        self._configure_tables()
        self._refresh_task_table()
        self._load_current_snapshot(initial=True)
        if should_auto_sync_on_start(self._current_snapshot_info):
            self._start_sync_thread()
        elif self.current_snapshot_id is not None:
            self._set_status(
                f"Loaded cached snapshot {self.current_snapshot_id}. Auto sync skipped because it is less than 24h old."
            )

    def action_sync(self) -> None:
        self._start_sync_thread(manual=True)

    def action_use_latest(self) -> None:
        if self.pending_snapshot_id is None:
            self._set_status("No newer snapshot available.")
            return

        self.current_snapshot_id = self.pending_snapshot_id
        self.pending_snapshot_id = None
        self._load_current_snapshot(initial=False)
        self._set_status(f"Switched to snapshot {self.current_snapshot_id}.")

    def action_edit_filters(self) -> None:
        self.push_screen(StateFilterScreen(self.failure_states), self._on_filter_screen_dismissed)

    def action_download_selected(self) -> None:
        request = self._selected_download_request()
        if request is None:
            self._set_status("No row selected for download.")
            return

        task_id = self._create_task(kind="download", status="queued", item=request.item_label, detail="Queued")
        self._download_queue.put(replace(request, task_id=task_id))
        self._refresh_task_table()
        self._set_status(f"Queued download for {request.item_label}.")
        self._start_download_worker()

    def action_queue_extract_selected(self) -> None:
        self._queue_selected(AdminActionKind.QUEUE_EXTRACT)

    def action_queue_mirror_selected(self) -> None:
        self._queue_selected(AdminActionKind.QUEUE_MIRROR)

    def action_apply_batch(self) -> None:
        if self._pending_batch is None or not self._pending_batch.targets:
            self._set_status("No pending batch to apply.")
            return
        if self._current_snapshot_info is None:
            self._set_status("No snapshot is loaded yet.")
            return
        validation_error = self._pending_batch_validation_error(self._pending_batch)
        if validation_error is not None:
            self._set_status(f"Cannot apply pending batch: {validation_error}")
            return
        if self._pending_batch.reason.strip():
            self._start_apply_thread(self._pending_batch)
            return
        self.push_screen(ReasonScreen(), self._on_reason_screen_dismissed)

    def action_clear_batch(self) -> None:
        if self._pending_batch is None:
            self._set_status("No pending batch to clear.")
            return
        self._pending_batch = None
        self._refresh_pending_batch()
        self._set_status("Cleared pending batch.")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table_id = self.focused.id if self.focused is not None else None
        row_key = event.row_key.value
        if table_id not in {"ipsw-failures", "ota-failures", "tasks"} or row_key is None:
            return

        self._active_table_id = table_id
        if table_id == "ipsw-failures":
            self._selected_ipsw_row_key = row_key if row_key in self._ipsw_rows_by_key else None
        elif table_id == "ota-failures":
            self._selected_ota_row_key = row_key if row_key in self._ota_rows_by_key else None
        elif table_id == "tasks":
            self._selected_task_id = row_key if row_key in self._tasks else None
        self._refresh_details()

    def _configure_tables(self) -> None:
        ipsw_table = cast(DataTable[str], self.query_one("#ipsw-failures", DataTable))
        ipsw_table.add_columns("last_modified", "state", "platform", "version", "build", "file_name", "artifact_key")

        ota_table = cast(DataTable[str], self.query_one("#ota-failures", DataTable))
        ota_table.add_columns("last_run_at", "state", "platform", "version", "build", "artifact_id", "ota_key")

        tasks_table = cast(DataTable[object], self.query_one("#tasks", DataTable))
        tasks_table.add_columns("type", "status", "item", "detail")

    def _load_current_snapshot(self, initial: bool) -> None:
        if self.current_snapshot_id is None:
            active_paths = active_snapshot_paths(self.cache_dir)
            if active_paths is not None:
                self.current_snapshot_id = active_paths.snapshot_id

        if self.current_snapshot_id is None:
            self._current_snapshot_info = None
            self._all_ipsw_rows_by_key = {}
            self._all_ota_rows_by_key = {}
            self._populate_ipsw_table([])
            self._populate_ota_table([])
            self.sub_title = self._subtitle()
            self._refresh_details()
            self._refresh_pending_batch()
            if initial:
                self._set_status("No cached snapshot found. Starting background sync…")
            return

        db_path = snapshot_paths(self.cache_dir, self.current_snapshot_id).db_path
        self._current_snapshot_info = load_snapshot_info(db_path)
        all_ipsw_rows = load_ipsw_rows(db_path)
        self._all_ipsw_rows_by_key = {_ipsw_row_key(row): row for row in all_ipsw_rows}
        visible_ipsw_rows = [row for row in all_ipsw_rows if row.processing_state in self.failure_states]
        self._populate_ipsw_table(visible_ipsw_rows)
        all_ota_rows = load_ota_rows(db_path)
        self._all_ota_rows_by_key = {_ota_row_key(row): row for row in all_ota_rows}
        visible_ota_rows = [row for row in all_ota_rows if row.processing_state in self.failure_states]
        self._populate_ota_table(visible_ota_rows)
        self._start_run_lookup_thread(all_ota_rows)
        self.sub_title = self._subtitle()
        self._refresh_details()
        self._refresh_pending_batch()
        if initial:
            self._set_status(f"Loaded cached snapshot {self.current_snapshot_id}.")

    def _populate_ipsw_table(self, rows: list[IpswSourceRow]) -> None:
        table = cast(DataTable[str], self.query_one("#ipsw-failures", DataTable))
        previous_row_key = self._selected_ipsw_row_key
        self._ipsw_rows = rows
        self._ipsw_rows_by_key = {}
        table.clear(columns=False)
        self._set_failure_table_title("ipsw", len(rows))
        if not rows:
            self._selected_ipsw_row_key = None
            table.add_row(EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, "No matching IPSW rows", EMPTY, key="empty-ipsw")
            self._ensure_active_table()
            return

        for row in rows:
            row_key = _ipsw_row_key(row)
            self._ipsw_rows_by_key[row_key] = row
            table.add_row(
                row.last_modified or EMPTY,
                row.processing_state.value,
                row.platform,
                row.version,
                row.build,
                row.file_name,
                row.artifact_key,
                key=row_key,
            )

        self._selected_ipsw_row_key = self._restore_row_selection(table, previous_row_key, list(self._ipsw_rows_by_key))
        self._ensure_active_table()

    def _populate_ota_table(self, rows: list[OtaArtifactRow]) -> None:
        table = cast(DataTable[str], self.query_one("#ota-failures", DataTable))
        previous_row_key = self._selected_ota_row_key
        self._ota_rows = rows
        self._ota_rows_by_key = {}
        table.clear(columns=False)
        self._set_failure_table_title("ota", len(rows))
        if not rows:
            self._selected_ota_row_key = None
            table.add_row(EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, "No matching OTA rows", EMPTY, key="empty-ota")
            self._ensure_active_table()
            return

        for row in rows:
            row_key = _ota_row_key(row)
            self._ota_rows_by_key[row_key] = row
            table.add_row(
                format_github_run_time(row.last_run, self._run_infos.get(row.last_run)),
                row.processing_state.value,
                row.platform,
                row.version,
                row.build,
                row.artifact_id,
                row.ota_key,
                key=row_key,
            )

        self._selected_ota_row_key = self._restore_row_selection(table, previous_row_key, list(self._ota_rows_by_key))
        self._ensure_active_table()

    def _restore_row_selection(
        self,
        table: DataTable[str],
        previous_row_key: str | None,
        current_row_keys: list[str],
    ) -> str | None:
        if not current_row_keys:
            return None

        target_row_key = previous_row_key if previous_row_key in current_row_keys else current_row_keys[0]
        table.move_cursor(row=table.get_row_index(target_row_key), animate=False, scroll=False)
        return target_row_key

    def _ensure_active_table(self) -> None:
        if self._active_table_id == "ipsw-failures" and self._selected_ipsw_row_key is not None:
            return
        if self._active_table_id == "ota-failures" and self._selected_ota_row_key is not None:
            return
        if self._active_table_id == "tasks" and self._selected_task_id is not None:
            return
        if self._selected_ipsw_row_key is not None:
            self._active_table_id = "ipsw-failures"
            return
        if self._selected_ota_row_key is not None:
            self._active_table_id = "ota-failures"
            return
        if self._selected_task_id is not None:
            self._active_table_id = "tasks"
            return
        self._active_table_id = None

    def _set_failure_table_title(self, platform: str, count: int) -> None:
        if platform == "ipsw":
            self.query_one("#ipsw-title", Static).update(format_failure_table_title("IPSW sources", count))
        elif platform == "ota":
            self.query_one("#ota-title", Static).update(format_failure_table_title("OTA artifacts", count))

    def _refresh_details(self) -> None:
        self.query_one("#details", Static).update(self._detail_text())

    def _refresh_pending_batch(self) -> None:
        self.query_one("#pending-batch", Static).update(self._pending_batch_text())

    def _pending_batch_text(self) -> str:
        if self._pending_batch is None:
            return batch_summary(None)

        lines = [batch_summary(self._pending_batch), "", "Preview:"]
        for target in self._pending_batch.targets[:8]:
            preview_text = self._preview_target(target)
            lines.append(f"  - {preview_text}")
        if len(self._pending_batch.targets) > 8:
            lines.append(f"  … and {len(self._pending_batch.targets) - 8} more")
        return "\n".join(lines)

    def _preview_target(self, target: IpswTarget | OtaTarget) -> str:
        batch = self._pending_batch
        if batch is None:
            return target_label(target)

        preview = preview_target_against_snapshot(
            batch.store,
            batch.action,
            target,
            self._all_ipsw_rows_by_key,
            self._all_ota_rows_by_key,
        )
        if preview.current_state is None or preview.row_label is None:
            return f"{target_label(target)} ({preview.note})"
        if preview.resulting_state is None:
            return f"{preview.row_label}: {preview.current_state.value} ({preview.note})"
        return f"{preview.row_label}: {preview.current_state.value} -> {preview.resulting_state.value} ({preview.note})"

    def _detail_text(self) -> str:
        if self._active_table_id == "tasks" and self._selected_task_id is not None:
            task = self._tasks.get(self._selected_task_id)
            if task is not None:
                return format_task_detail(task)

        if self._active_table_id == "ota-failures" and self._selected_ota_row_key is not None:
            row = self._ota_rows_by_key.get(self._selected_ota_row_key)
            if row is not None:
                return format_ota_detail(row, self._run_infos.get(row.last_run))

        if self._active_table_id == "ipsw-failures" and self._selected_ipsw_row_key is not None:
            row = self._ipsw_rows_by_key.get(self._selected_ipsw_row_key)
            if row is not None:
                return format_ipsw_detail(row)

        if self._selected_ipsw_row_key is not None:
            row = self._ipsw_rows_by_key.get(self._selected_ipsw_row_key)
            if row is not None:
                return format_ipsw_detail(row)

        if self._selected_ota_row_key is not None:
            row = self._ota_rows_by_key.get(self._selected_ota_row_key)
            if row is not None:
                return format_ota_detail(row, self._run_infos.get(row.last_run))

        if self._selected_task_id is not None:
            task = self._tasks.get(self._selected_task_id)
            if task is not None:
                return format_task_detail(task)

        return "No row selected. Use arrow keys to move in a table, then press 'd' to queue a download."

    def _selected_download_request(self) -> DownloadQueueItem | None:
        if self._active_table_id == "ipsw-failures" and self._selected_ipsw_row_key is not None:
            row = self._ipsw_rows_by_key.get(self._selected_ipsw_row_key)
            if row is not None:
                return DownloadQueueItem(task_id="", item_label=row.file_name, row=row)

        if self._active_table_id == "ota-failures" and self._selected_ota_row_key is not None:
            row = self._ota_rows_by_key.get(self._selected_ota_row_key)
            if row is not None:
                return DownloadQueueItem(task_id="", item_label=row.artifact_id, row=row)

        return None

    def _selected_batch_target(self) -> tuple[AdminStore, IpswTarget | OtaTarget] | None:
        if self._active_table_id == "ipsw-failures" and self._selected_ipsw_row_key is not None:
            row = self._ipsw_rows_by_key.get(self._selected_ipsw_row_key)
            if row is not None:
                return AdminStore.IPSW, IpswTarget(artifact_key=row.artifact_key, link=row.link)

        if self._active_table_id == "ota-failures" and self._selected_ota_row_key is not None:
            row = self._ota_rows_by_key.get(self._selected_ota_row_key)
            if row is not None:
                return AdminStore.OTA, OtaTarget(ota_key=row.ota_key)

        return None

    def _queue_selected(self, action: AdminActionKind) -> None:
        selected = self._selected_batch_target()
        if selected is None:
            self._set_status("No row selected to add to the pending batch.")
            return

        store, target = selected
        if self._pending_batch is not None and (
            self._pending_batch.store != store or self._pending_batch.action != action
        ):
            self._set_status("Pending batch already targets a different store or action. Clear it first.")
            return

        preview = preview_target_against_snapshot(
            store,
            action,
            target,
            self._all_ipsw_rows_by_key,
            self._all_ota_rows_by_key,
        )
        if not preview.allowed or preview.resulting_state is None:
            self._set_status(
                f"Cannot add {target_label(target)} to {action_label(action).lower()} batch: {preview.note}."
            )
            return

        if self._pending_batch is None:
            self._pending_batch = PendingBatch(store=store, action=action, targets=(target,))
        else:
            previous_count = len(self._pending_batch.targets)
            self._pending_batch = add_target_to_pending_batch(self._pending_batch, target)
            if len(self._pending_batch.targets) == previous_count:
                self._set_status(f"{target_label(target)} is already in the pending batch.")
                return

        self._refresh_pending_batch()
        self._set_status(f"Added {target_label(target)} to {action_label(action).lower()} batch.")

    def _start_sync_thread(self, manual: bool = False, activate_latest: bool = False) -> None:
        if self._sync_thread is not None and self._sync_thread.is_alive():
            if manual:
                self._set_status("Sync already running…")
            if activate_latest:
                self._sync_activate_latest = True
            return

        if activate_latest:
            self._sync_activate_latest = True

        task_id = self._create_task(
            kind="sync",
            status="queued",
            item="manual" if manual else "startup",
            detail="Queued",
        )
        prefix = "Manual sync requested" if manual else "Starting background sync"
        self._set_status(f"{prefix}…")
        self._sync_thread = threading.Thread(target=self._run_sync, args=(task_id,), daemon=True)
        self._sync_thread.start()

    def _run_sync(self, task_id: str) -> None:
        if not self._sync_lock.acquire(blocking=False):
            return

        self._task_update_from_thread(task_id, status="running", progress_detail="Running")
        try:
            result = run_sync(
                self.cache_dir, status_callback=lambda message: self._sync_progress_from_thread(task_id, message)
            )
        except AdminSyncError as exc:
            self._task_update_from_thread(task_id, status="failed", result_detail=str(exc))
            self._status_from_thread(f"Sync failed: {exc}")
            return
        except Exception as exc:  # pragma: no cover - defensive UI guard
            self._task_update_from_thread(task_id, status="failed", result_detail=str(exc))
            self._status_from_thread(f"Unexpected sync failure: {exc}")
            return
        finally:
            self._sync_lock.release()

        self.call_from_thread(self._handle_sync_result, result, task_id)

    def _sync_progress_from_thread(self, task_id: str, message: str) -> None:
        self.call_from_thread(self._sync_progress, task_id, message)

    def _sync_progress(self, task_id: str, message: str) -> None:
        self._update_task(task_id, status="running", progress_detail=message)
        self._set_status(message)

    def _handle_sync_result(self, result: SyncResult, task_id: str) -> None:
        activate_latest = self._sync_activate_latest
        self._sync_activate_latest = False

        if self.current_snapshot_id is None:
            self.current_snapshot_id = result.snapshot_id
            self.pending_snapshot_id = None
            self._load_current_snapshot(initial=False)
            self._update_task(task_id, status="done", result_detail="Initial snapshot loaded")
            self._set_status(f"Initial snapshot {result.snapshot_id} loaded.")
            return

        if result.snapshot_id == self.current_snapshot_id:
            self.pending_snapshot_id = None
            self._update_task(task_id, status="done", result_detail="Already on the latest snapshot")
            self._set_status(f"Already on latest snapshot {result.snapshot_id}.")
            return

        if activate_latest:
            self.current_snapshot_id = result.snapshot_id
            self.pending_snapshot_id = None
            self._load_current_snapshot(initial=False)
            self._update_task(task_id, status="done", result_detail=f"Loaded latest snapshot {result.snapshot_id}")
            self._set_status(f"Loaded latest snapshot {result.snapshot_id}.")
            return

        self.pending_snapshot_id = result.snapshot_id
        self.sub_title = self._subtitle()
        self._update_task(task_id, status="done", result_detail=f"New snapshot {result.snapshot_id} is ready")
        self._set_status(f"New snapshot {result.snapshot_id} is ready. Press 'u' to switch.")

    def _start_apply_thread(self, batch: PendingBatch) -> None:
        validation_error = self._pending_batch_validation_error(batch)
        if validation_error is not None:
            self._set_status(f"Cannot apply pending batch: {validation_error}")
            return

        if self._apply_thread is not None and self._apply_thread.is_alive():
            self._set_status("Apply already running…")
            return

        task_id = self._create_task(
            kind="apply",
            status="queued",
            item=f"{batch.store.value}:{batch.action.value}",
            detail=f"Queued ({len(batch.targets)} targets)",
        )
        self._set_status("Starting admin apply…")
        self._apply_thread = threading.Thread(target=self._run_apply_batch, args=(batch, task_id), daemon=True)
        self._apply_thread.start()

    def _run_apply_batch(self, batch: PendingBatch, task_id: str) -> None:
        snapshot_info = self._current_snapshot_info
        if snapshot_info is None:
            self._task_update_from_thread(task_id, status="failed", result_detail="No snapshot is loaded")
            self._status_from_thread("Cannot apply without a loaded snapshot.")
            return

        try:
            request = bind_pending_batch(batch, snapshot_info)
        except ValueError as exc:
            self._task_update_from_thread(task_id, status="failed", result_detail=str(exc))
            self._status_from_thread(str(exc))
            return

        self._task_update_from_thread(task_id, status="running", progress_detail="Running")
        try:
            result = run_apply(
                request, status_callback=lambda message: self._apply_progress_from_thread(task_id, message)
            )
        except AdminApplyError as exc:
            self._task_update_from_thread(task_id, status="failed", result_detail=str(exc))
            self._status_from_thread(f"Admin apply failed: {exc}")
            return
        except Exception as exc:  # pragma: no cover - defensive UI guard
            self._task_update_from_thread(task_id, status="failed", result_detail=str(exc))
            self._status_from_thread(f"Unexpected admin apply failure: {exc}")
            return

        self.call_from_thread(self._handle_apply_result, batch, result, task_id)

    def _apply_progress_from_thread(self, task_id: str, message: str) -> None:
        self.call_from_thread(self._apply_progress, task_id, message)

    def _apply_progress(self, task_id: str, message: str) -> None:
        self._update_task(task_id, status="running", progress_detail=message)
        self._set_status(message)

    def _handle_apply_result(self, batch: PendingBatch, result: ApplyBatchResult, task_id: str) -> None:
        status = "done"
        if result.status == ApplyBatchStatus.APPLIED_WITH_WORKER_WARNING:
            status = "warning"
        elif result.status in {
            ApplyBatchStatus.STALE_GENERATION,
            ApplyBatchStatus.VALIDATION_FAILED,
            ApplyBatchStatus.INTERNAL_ERROR,
        }:
            status = "failed"

        self._update_task(task_id, status=status, result_detail=result.message)
        if result.status in {ApplyBatchStatus.APPLIED, ApplyBatchStatus.APPLIED_WITH_WORKER_WARNING}:
            self._pending_batch = None
            self._refresh_pending_batch()
            self._set_status(result.message)
            self._start_sync_thread(activate_latest=True)
            return

        self._pending_batch = batch
        self._refresh_pending_batch()
        if result.status == ApplyBatchStatus.STALE_GENERATION:
            self._set_status(f"{result.message} Refreshing snapshot now…")
            self._start_sync_thread(activate_latest=True)
            return
        self._set_status(result.message)

    def _start_download_worker(self) -> None:
        with self._download_worker_lock:
            if self._download_worker_thread is not None and self._download_worker_thread.is_alive():
                return

            self._download_worker_thread = threading.Thread(target=self._run_download_worker, daemon=True)
            self._download_worker_thread.start()

    def _run_download_worker(self) -> None:
        try:
            while True:
                try:
                    item = self._download_queue.get(timeout=0.2)
                except Empty:
                    if self._finish_download_worker_if_idle():
                        return
                    continue

                self._task_update_from_thread(item.task_id, status="running", progress_detail="Downloading")
                self._status_from_thread(f"Downloading {item.item_label}…")
                try:
                    result = _download_selected_item(
                        item.row,
                        self.cache_dir,
                        status_callback=lambda message, tid=item.task_id: self._download_progress_from_thread(
                            tid, message
                        ),
                    )
                except Exception as exc:
                    self._task_update_from_thread(item.task_id, status="failed", result_detail=str(exc))
                    self._status_from_thread(f"Download failed for {item.item_label}: {exc}")
                else:
                    self._handle_download_result_from_thread(item, result)
                finally:
                    self._download_queue.task_done()
        finally:
            with self._download_worker_lock:
                if self._download_worker_thread is threading.current_thread():
                    self._download_worker_thread = None

    def _finish_download_worker_if_idle(self) -> bool:
        with self._download_worker_lock:
            if not self._download_queue.empty():
                return False
            if self._download_worker_thread is threading.current_thread():
                self._download_worker_thread = None
            return True

    def _download_progress_from_thread(self, task_id: str, message: str) -> None:
        self.call_from_thread(self._update_task, task_id, progress_detail=message)

    def _handle_download_result_from_thread(self, item: DownloadQueueItem, result: ArtifactDownloadResult) -> None:
        self.call_from_thread(self._handle_download_result, item, result)

    def _handle_download_result(self, item: DownloadQueueItem, result: ArtifactDownloadResult) -> None:
        task_status = "done" if result.verified else "warning"
        self._update_task(item.task_id, status=task_status, result_detail=result.message)
        if result.verified:
            self._set_status(f"Download complete for {item.item_label}.")
        else:
            self._set_status(f"Download complete for {item.item_label} (unverified).")

    def _start_run_lookup_thread(self, ota_rows: list[OtaArtifactRow]) -> None:
        missing_run_ids = [row.last_run for row in ota_rows if row.last_run > 0 and row.last_run not in self._run_infos]
        if not missing_run_ids:
            return
        if self._run_lookup_thread is not None and self._run_lookup_thread.is_alive():
            return

        self._run_lookup_thread = threading.Thread(
            target=self._resolve_run_infos,
            args=(tuple(dict.fromkeys(missing_run_ids)),),
            daemon=True,
        )
        self._run_lookup_thread.start()

    def _resolve_run_infos(self, run_ids: tuple[int, ...]) -> None:
        try:
            run_infos = ensure_github_run_infos(self.cache_dir, run_ids)
        except Exception:  # pragma: no cover - best effort UI enhancement
            return
        self.call_from_thread(self._merge_run_infos, run_infos)

    def _merge_run_infos(self, run_infos: dict[int, GithubRunInfo]) -> None:
        self._run_infos.update(run_infos)
        if self._ota_rows:
            self._populate_ota_table(self._ota_rows)
            self._refresh_details()

    def _create_task(self, kind: str, status: str, item: str, detail: str) -> str:
        self._task_counter += 1
        task_id = f"task-{self._task_counter}"
        self._task_order.append(task_id)
        self._tasks[task_id] = BackgroundTaskEntry(
            task_id=task_id,
            kind=kind,
            status=status,
            item=item,
            progress_detail=detail,
        )
        self._refresh_task_table()
        return task_id

    def _update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress_detail: str | None = None,
        result_detail: str | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return

        self._tasks[task_id] = BackgroundTaskEntry(
            task_id=task.task_id,
            kind=task.kind,
            status=status or task.status,
            item=task.item,
            progress_detail=progress_detail or task.progress_detail,
            result_detail=result_detail if result_detail is not None else task.result_detail,
        )
        self._refresh_task_table()
        if self._selected_task_id == task_id:
            self._refresh_details()

    def _task_update_from_thread(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress_detail: str | None = None,
        result_detail: str | None = None,
    ) -> None:
        self.call_from_thread(
            self._update_task,
            task_id,
            status=status,
            progress_detail=progress_detail,
            result_detail=result_detail,
        )

    def _refresh_task_table(self) -> None:
        table = cast(DataTable[object], self.query_one("#tasks", DataTable))
        previous_task_id = self._selected_task_id
        table.clear(columns=False)
        if not self._task_order:
            self._selected_task_id = None
            table.add_row(EMPTY, EMPTY, EMPTY, "No background tasks yet", key="empty-task")
            self._ensure_active_table()
            return

        task_ids = list(reversed(self._task_order[-MAX_TASK_ROWS:]))
        for task_id in task_ids:
            task = self._tasks[task_id]
            table.add_row(
                task.kind,
                format_task_status(task.status),
                task.item,
                summarize_task_detail(task),
                key=task.task_id,
            )

        self._selected_task_id = self._restore_task_selection(table, previous_task_id, task_ids)
        self._ensure_active_table()

    def _pending_batch_validation_error(self, batch: PendingBatch) -> str | None:
        issues = validate_pending_batch_against_snapshot(
            batch,
            self._all_ipsw_rows_by_key,
            self._all_ota_rows_by_key,
        )
        if not issues:
            return None
        return format_validation_issues(issues)

    def _status_from_thread(self, message: str) -> None:
        self.call_from_thread(self._set_status, message)

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(self._status_text(message))

    def _on_filter_screen_dismissed(self, result: tuple[ArtifactProcessingState, ...] | None) -> None:
        if result is None:
            self._set_status("State filter unchanged.")
            return

        self.failure_states = result
        self._load_current_snapshot(initial=False)
        self._set_status(f"Updated state filter to {self._state_filter_label()}.")

    def _on_reason_screen_dismissed(self, result: str | None) -> None:
        if result is None:
            self._set_status("Admin apply cancelled because no reason was provided.")
            return
        if self._pending_batch is None:
            self._set_status("No pending batch to apply.")
            return
        self._pending_batch = with_pending_batch_reason(self._pending_batch, result)
        self._refresh_pending_batch()
        validation_error = self._pending_batch_validation_error(self._pending_batch)
        if validation_error is not None:
            self._set_status(f"Cannot apply pending batch: {validation_error}")
            return
        self._start_apply_thread(self._pending_batch)

    def _subtitle(self) -> str:
        current = self.current_snapshot_id or "none"
        pending = self.pending_snapshot_id or "none"
        states = self._state_filter_label()
        if self._current_snapshot_info is None:
            return f"current={current} | pending={pending} | states={states}"
        return (
            f"current={current} | pending={pending} | ipsw={self._current_snapshot_info.ipsw_generation} | "
            f"ota={self._current_snapshot_info.ota_generation} | states={states}"
        )

    def _state_filter_label(self) -> str:
        if not self.failure_states:
            return "none"
        return ",".join(state.value for state in self.failure_states)

    def _restore_task_selection(
        self,
        table: DataTable[object],
        previous_task_id: str | None,
        task_ids: list[str],
    ) -> str | None:
        if not task_ids:
            return None

        target_task_id = previous_task_id if previous_task_id in task_ids else task_ids[0]
        table.move_cursor(row=table.get_row_index(target_task_id), animate=False, scroll=False)
        return target_task_id

    def _status_text(self, message: str) -> str:
        return (
            f"{message}\n"
            "Press 's' to refresh, 'u' to switch to the latest snapshot, 'f' to edit state filters, 'd' to queue a download, 'e'/'m' to build a rerun batch, 'a' to apply it, 'c' to clear it, 'q' to quit."
        )


def _download_selected_item(
    row: IpswSourceRow | OtaArtifactRow,
    cache_dir: Path,
    status_callback: Callable[[str], None] | None = None,
) -> ArtifactDownloadResult:
    if isinstance(row, IpswSourceRow):
        return download_ipsw_to_cache(row, cache_dir, status_callback=status_callback)
    return download_ota_to_cache(row, cache_dir, status_callback=status_callback)


def format_failure_table_title(label: str, count: int) -> str:
    return f"{label} ({count})"


def format_task_status(status: str) -> Text:
    label, style = {
        "queued": ("… queued", "yellow"),
        "running": ("↻ running", "blue"),
        "done": ("✓ done", "green"),
        "warning": ("! warning", "yellow"),
        "failed": ("✗ failed", "red"),
    }.get(status, (status, "white"))
    return Text(label, style=style)


def summarize_task_detail(task: BackgroundTaskEntry, max_length: int = 56) -> str:
    detail = task.result_detail or task.progress_detail
    if len(detail) <= max_length:
        return detail
    return f"{detail[: max_length - 1]}…"


def format_task_detail(task: BackgroundTaskEntry) -> str:
    lines = [
        "type: Background task",
        f"task_id: {task.task_id}",
        f"kind: {task.kind}",
        f"status: {task.status}",
        f"item: {task.item}",
        f"latest_progress: {task.progress_detail}",
    ]
    if task.result_detail is not None:
        lines.append(f"result: {task.result_detail}")
    return "\n".join(lines)


def format_ipsw_detail(row: IpswSourceRow) -> str:
    return "\n".join(
        [
            "type: IPSW source",
            f"state: {row.processing_state.value}",
            f"platform: {row.platform}",
            f"version: {row.version}",
            f"build: {row.build}",
            f"artifact_key: {row.artifact_key}",
            f"file_name: {row.file_name}",
            f"last_modified: {row.last_modified or EMPTY}",
            f"last_run: #{row.last_run}",
            f"sha1: {row.sha1 or EMPTY}",
            f"mirror_path: {row.mirror_path or EMPTY}",
            f"link: {row.link}",
        ]
    )


def format_ota_detail(row: OtaArtifactRow, run_info: GithubRunInfo | None) -> str:
    started_at = format_iso_timestamp(run_info.started_at) if run_info is not None and run_info.started_at else EMPTY
    updated_at = format_iso_timestamp(run_info.updated_at) if run_info is not None and run_info.updated_at else EMPTY
    run_url = run_info.url if run_info is not None and run_info.url else EMPTY
    run_title = run_info.display_title if run_info is not None and run_info.display_title else EMPTY
    return "\n".join(
        [
            "type: OTA artifact",
            f"state: {row.processing_state.value}",
            f"platform: {row.platform}",
            f"version: {row.version}",
            f"build: {row.build}",
            f"ota_key: {row.ota_key}",
            f"artifact_id: {row.artifact_id}",
            f"last_run: #{row.last_run}",
            f"last_run_at: {format_github_run_time(row.last_run, run_info)}",
            f"hash: {row.hash}",
            f"hash_algorithm: {row.hash_algorithm}",
            f"run_title: {run_title}",
            f"run_started_at: {started_at}",
            f"run_updated_at: {updated_at}",
            f"run_url: {run_url}",
            f"download_path: {row.download_path or EMPTY}",
            f"url: {row.url}",
        ]
    )


def _ipsw_row_key(row: IpswSourceRow) -> str:
    return f"{row.artifact_key}::{row.link}"


def _ota_row_key(row: OtaArtifactRow) -> str:
    return row.ota_key


def should_auto_sync_on_start(
    snapshot_info: SnapshotInfo | None,
    now: datetime | None = None,
    max_age: timedelta = AUTO_SYNC_MAX_AGE,
) -> bool:
    if snapshot_info is None:
        return True

    if now is None:
        now = datetime.now(UTC)

    try:
        created_at = datetime.fromisoformat(snapshot_info.created_at)
    except ValueError:
        return True

    return (now - created_at) >= max_age


def launch_tui(cache_dir: Path, failure_states: tuple[ArtifactProcessingState, ...] = DEFAULT_FAILURE_STATES) -> None:
    app = AdminTui(cache_dir=cache_dir, failure_states=failure_states)
    app.run()
