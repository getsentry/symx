from __future__ import annotations

from pathlib import Path

import typer

from symx.artifacts.report import (
    ArtifactReportError,
    build_parity_report_from_files,
    report_to_json,
    write_parity_report_json,
)

artifacts_app = typer.Typer(help="Artifact metadata migration and validation helpers")
v2_app = typer.Typer(help="metadata-v2 shadow-model helpers")
artifacts_app.add_typer(v2_app, name="v2")


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
