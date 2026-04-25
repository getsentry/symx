from __future__ import annotations

import os
from pathlib import Path

import typer

from symx.admin.actions import ApplyBatchRequest, ApplyBatchStatus
from symx.admin.db import DEFAULT_FAILURE_STATES, default_cache_dir
from symx.admin.remote import append_apply_summary, execute_apply_request, write_apply_result
from symx.admin.sync import AdminSyncError, run_sync
from symx.admin.tui import launch_tui
from symx.model import ArtifactProcessingState

admin_app = typer.Typer(invoke_without_command=True, no_args_is_help=False, help="Admin TUI and local meta cache")


@admin_app.callback(invoke_without_command=True)
def admin_callback(
    ctx: typer.Context,
    cache_dir: Path = typer.Option(default_cache_dir(), "--cache-dir", help="Path to the local admin cache directory"),
    failure_state: list[ArtifactProcessingState] = typer.Option(
        [],
        "--failure-state",
        help="Processing states to include; repeat the option to narrow the tables (defaults to failure states)",
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        launch_tui(cache_dir, _normalize_failure_states(failure_state))


@admin_app.command("tui")
def tui_command(
    cache_dir: Path = typer.Option(default_cache_dir(), "--cache-dir", help="Path to the local admin cache directory"),
    failure_state: list[ArtifactProcessingState] = typer.Option(
        [],
        "--failure-state",
        help="Processing states to include; repeat the option to narrow the tables (defaults to failure states)",
    ),
) -> None:
    launch_tui(cache_dir, _normalize_failure_states(failure_state))


@admin_app.command("sync")
def sync_command(
    cache_dir: Path = typer.Option(default_cache_dir(), "--cache-dir", help="Path to the local admin cache directory"),
) -> None:
    try:
        result = run_sync(cache_dir, status_callback=typer.echo)
    except AdminSyncError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    snapshot_status = "new" if result.is_new_snapshot else "existing"
    typer.echo(
        (
            "Done. "
            f"snapshot={result.snapshot_id} ({snapshot_status}); "
            f"ipsw_generation={result.ipsw_generation}; ota_generation={result.ota_generation}"
        )
    )


@admin_app.command("apply-batch", hidden=True)
def apply_batch_command(
    storage: str = typer.Option(..., "--storage", help="GCS storage URI"),
    request_json: str = typer.Option(..., "--request-json", help="Serialized apply request JSON"),
    result_path: Path = typer.Option(..., "--result-path", help="Where to write the apply result JSON"),
) -> None:
    try:
        request = ApplyBatchRequest.from_json(request_json)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    result = execute_apply_request(storage, request, status_callback=typer.echo)
    write_apply_result(result_path, result)

    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    append_apply_summary(Path(step_summary) if step_summary else None, result)

    if result.status in {ApplyBatchStatus.APPLIED, ApplyBatchStatus.APPLIED_WITH_WORKER_WARNING}:
        return
    raise typer.Exit(code=1)


def _normalize_failure_states(failure_states: list[ArtifactProcessingState]) -> tuple[ArtifactProcessingState, ...]:
    if not failure_states:
        return DEFAULT_FAILURE_STATES

    normalized = dict.fromkeys(failure_states)
    return tuple(normalized)
