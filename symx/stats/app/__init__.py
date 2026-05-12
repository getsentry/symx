from __future__ import annotations

from pathlib import Path

import typer

from symx.admin.db import default_cache_dir
from symx.stats.coverage import CoverageReportError, build_coverage_report, resolve_snapshot_db, write_coverage_html

stats_app = typer.Typer(help="Generate snapshot-based stats and reports")


@stats_app.command("coverage-html")
def coverage_html_command(
    output: Path = typer.Option(..., "--output", "-o", help="Where to write the generated HTML file"),
    cache_dir: Path = typer.Option(
        default_cache_dir(),
        "--cache-dir",
        help="Path to the local admin cache directory used to resolve the active snapshot",
    ),
    db_path: Path | None = typer.Option(
        None,
        "--db-path",
        help="Path to a specific snapshot.db (defaults to the active snapshot from --cache-dir)",
    ),
) -> None:
    try:
        resolved_db_path = resolve_snapshot_db(cache_dir, db_path)
        report = build_coverage_report(resolved_db_path)
    except CoverageReportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    write_coverage_html(output, report)
    typer.echo(f"Wrote coverage HTML to {output}")
