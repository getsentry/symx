from __future__ import annotations

import json
from pathlib import Path

import typer

from symx.artifacts.report import (
    ArtifactReportError,
    build_parity_report_from_files,
    report_to_json,
    write_parity_report_json,
)
from symx.artifacts.storage import ArtifactGcsPrefixStore, ArtifactStorageError, BootstrapResult, SnapshotViewResult

artifacts_app = typer.Typer(help="Artifact metadata migration and validation helpers")
v2_app = typer.Typer(help="metadata-v2 shadow-model helpers")
artifacts_app.add_typer(v2_app, name="v2")


@v2_app.command("bootstrap")
def bootstrap_command(
    storage: str = typer.Option(..., "--storage", "-s", help="URI to the GCS bucket containing legacy metadata"),
    prefix: str = typer.Option(..., "--prefix", help="Experiment prefix to write normalized metadata under"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Read and report without writing v2 objects"),
    max_workers: int = typer.Option(16, "--max-workers", help="Maximum concurrent GCS object writes"),
    allow_non_experiment_prefix: bool = typer.Option(
        False,
        "--allow-non-experiment-prefix",
        help="Allow prefixes outside experiments/ for explicit cutover testing",
    ),
) -> None:
    """Bootstrap normalized metadata objects from legacy GCS metadata."""

    if not allow_non_experiment_prefix and not prefix.strip("/").startswith("experiments/"):
        typer.echo("Refusing to write outside experiments/ without --allow-non-experiment-prefix", err=True)
        raise typer.Exit(code=1)

    try:
        store = ArtifactGcsPrefixStore.from_storage_uri(storage, prefix, connection_pool_size=max_workers)
        result = store.bootstrap(dry_run=dry_run, max_workers=max_workers)
    except ArtifactStorageError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(_bootstrap_summary_json(result).rstrip())
    if not result.parity_report.ok:
        raise typer.Exit(code=2)


@v2_app.command("snapshot")
def snapshot_command(
    storage: str = typer.Option(..., "--storage", "-s", help="URI to the GCS bucket containing legacy metadata"),
    prefix: str = typer.Option(..., "--prefix", help="Experiment prefix to write the snapshot view under"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Read and report without writing the snapshot DB"),
    allow_non_experiment_prefix: bool = typer.Option(
        False,
        "--allow-non-experiment-prefix",
        help="Allow prefixes outside experiments/ for explicit cutover testing",
    ),
) -> None:
    """Materialize a single SQLite snapshot DB for a metadata-v2 prefix."""

    if not allow_non_experiment_prefix and not prefix.strip("/").startswith("experiments/"):
        typer.echo("Refusing to write outside experiments/ without --allow-non-experiment-prefix", err=True)
        raise typer.Exit(code=1)

    try:
        store = ArtifactGcsPrefixStore.from_storage_uri(storage, prefix)
        result = store.write_snapshot_view(dry_run=dry_run)
    except ArtifactStorageError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(_snapshot_summary_json(result).rstrip())


@v2_app.command("report")
def report_command(
    ipsw_meta: Path = typer.Option(..., "--ipsw-meta", help="Path to legacy ipsw_meta.json", exists=True),
    ota_meta: Path = typer.Option(..., "--ota-meta", help="Path to legacy ota_image_meta.json", exists=True),
    output: Path | None = typer.Option(None, "--output", "-o", help="Optional JSON output path"),
) -> None:
    """Build a local v1-vs-v2 parity report from legacy metadata JSON files."""

    try:
        report = build_parity_report_from_files(ipsw_meta, ota_meta)
    except ArtifactReportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    if output is not None:
        write_parity_report_json(report, output)
        typer.echo(f"Wrote artifact parity report to {output}")
    else:
        typer.echo(report_to_json(report).rstrip())

    if not report.ok:
        raise typer.Exit(code=2)


def _snapshot_summary_json(result: SnapshotViewResult) -> str:
    payload = {
        "dry_run": result.dry_run,
        "prefix": result.prefix,
        "snapshot_db_path": result.snapshot_db_path,
        "snapshot_counts": result.snapshot_counts.model_dump(),
        "written_object_count": result.written_object_count,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _bootstrap_summary_json(result: BootstrapResult) -> str:
    payload = {
        "dry_run": result.dry_run,
        "prefix": result.manifest.prefix,
        "artifact_count": result.manifest.artifact_count,
        "detail_count": result.manifest.detail_count,
        "parity_ok": result.manifest.parity_ok,
        "parity_mismatch_count": result.manifest.parity_mismatch_count,
        "written_object_count": result.written_object_count,
        "sample_written_objects": result.sample_written_objects,
        "manifest_path": result.manifest.manifest_path,
        "parity_report_path": result.manifest.parity_report_path,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
