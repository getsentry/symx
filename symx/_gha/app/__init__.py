import json
import subprocess
import sys
from datetime import datetime
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

gha_app = typer.Typer(help="Query GitHub Actions workflow runs")
console = Console()


def parse_datetime(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def calculate_duration_minutes(started_at: str, updated_at: str) -> float:
    start = parse_datetime(started_at)
    end = parse_datetime(updated_at)
    return (end - start).total_seconds() / 60


def format_duration(minutes: float) -> str:
    if minutes < 1:
        return f"{minutes * 60:.0f}s"
    elif minutes < 60:
        return f"{minutes:.1f}m"
    else:
        hours = int(minutes // 60)
        mins = minutes % 60
        return f"{hours}h {mins:.0f}m"


def get_workflow_runs(
    workflow: str,
    limit: int = 100,
    branch: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    cmd = [
        "gh",
        "run",
        "list",
        "--workflow",
        workflow,
        "--limit",
        str(limit),
        "--json",
        "number,databaseId,name,conclusion,status,startedAt,updatedAt,url,displayTitle,headBranch,event",
    ]

    if branch:
        cmd.extend(["--branch", branch])
    if status:
        cmd.extend(["--status", status])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]Error fetching runs:[/red] {result.stderr}")
        sys.exit(1)

    return json.loads(result.stdout)


def get_run_log(run_id: int) -> str:
    result = subprocess.run(
        ["gh", "run", "view", str(run_id), "--log"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


@gha_app.command("runs")
def list_runs(
    workflow: str = typer.Argument(..., help="Workflow name or filename"),
    min_duration: Optional[float] = typer.Option(
        None,
        "--min-duration",
        "-d",
        help="Minimum duration in minutes",
    ),
    max_duration: Optional[float] = typer.Option(
        None,
        "--max-duration",
        help="Maximum duration in minutes",
    ),
    conclusion: Optional[str] = typer.Option(
        None,
        "--conclusion",
        "-c",
        help="Filter by conclusion (success, failure, cancelled, etc.)",
    ),
    branch: Optional[str] = typer.Option(
        None,
        "--branch",
        "-b",
        help="Filter by branch",
    ),
    limit: int = typer.Option(
        100,
        "--limit",
        "-L",
        help="Maximum number of runs to fetch",
    ),
    search_log: Optional[str] = typer.Option(
        None,
        "--grep",
        "-g",
        help="Search for string in run logs (slow, fetches logs for each run)",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
) -> None:
    """
    List workflow runs with duration and status filtering.

    Examples:

        symx gha runs "Mirror OTA images" --min-duration 10

        symx gha runs "Extract OTA symbols" -d 30 -c failure

        symx gha runs "Code checks" --grep "error" -L 20
    """
    runs = get_workflow_runs(workflow, limit=limit, branch=branch)

    filtered_runs: list[dict[str, Any]] = []
    for run in runs:
        if not run.get("startedAt") or not run.get("updatedAt"):
            continue

        duration = calculate_duration_minutes(run["startedAt"], run["updatedAt"])
        run["duration_minutes"] = duration

        if min_duration is not None and duration < min_duration:
            continue
        if max_duration is not None and duration > max_duration:
            continue

        if conclusion is not None and run.get("conclusion") != conclusion:
            continue

        filtered_runs.append(run)

    if search_log:
        console.print(f"[yellow]Searching logs for '{search_log}'...[/yellow]")
        matching_runs: list[dict[str, Any]] = []
        for run in filtered_runs:
            log = get_run_log(run["databaseId"])
            if search_log.lower() in log.lower():
                matching_lines = [line.strip() for line in log.split("\n") if search_log.lower() in line.lower()]
                run["matching_lines"] = matching_lines[:5]
                matching_runs.append(run)
        filtered_runs = matching_runs

    if output_json:
        print(json.dumps(filtered_runs, indent=2, default=str))
        return

    if not filtered_runs:
        console.print("[yellow]No matching runs found.[/yellow]")
        return

    table = Table(title=f"Workflow Runs: {workflow}")
    table.add_column("Run ID", style="cyan", justify="right")
    table.add_column("Title", style="white", max_width=40)
    table.add_column("Branch", style="blue")
    table.add_column("Status", style="yellow")
    table.add_column("Conclusion")
    table.add_column("Duration", justify="right")
    table.add_column("Started", style="dim")

    for run in filtered_runs:
        conclusion_style = {
            "success": "green",
            "failure": "red",
            "cancelled": "yellow",
            "skipped": "dim",
        }.get(run.get("conclusion", ""), "white")

        started = parse_datetime(run["startedAt"]).strftime("%Y-%m-%d %H:%M")

        table.add_row(
            str(run["databaseId"]),
            run.get("displayTitle", "")[:40],
            run.get("headBranch", "")[:20],
            run.get("status", ""),
            f"[{conclusion_style}]{run.get('conclusion', '-')}[/{conclusion_style}]",
            format_duration(run["duration_minutes"]),
            started,
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(filtered_runs)} runs[/dim]")

    if search_log:
        console.print(f"\n[bold]Log matches for '{search_log}':[/bold]")
        for run in filtered_runs[:5]:
            if run.get("matching_lines"):
                console.print(f"\n[cyan]Run {run['databaseId']}:[/cyan]")
                for line in run["matching_lines"]:
                    console.print(f"  {line[:120]}")


@gha_app.command("workflows")
def list_workflows() -> None:
    result = subprocess.run(
        ["gh", "workflow", "list", "--json", "name,state,id"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]Error:[/red] {result.stderr}")
        sys.exit(1)

    workflows = json.loads(result.stdout)

    table = Table(title="Available Workflows")
    table.add_column("Name", style="cyan")
    table.add_column("State", style="green")
    table.add_column("ID", style="dim")

    for wf in workflows:
        state_style = "green" if wf["state"] == "active" else "red"
        table.add_row(
            wf["name"],
            f"[{state_style}]{wf['state']}[/{state_style}]",
            str(wf["id"]),
        )

    console.print(table)


@gha_app.command("view-log")
def view_log(
    run_id: int = typer.Argument(..., help="Run ID (databaseId from 'runs' output) to view logs for"),
    grep: Optional[str] = typer.Option(
        None,
        "--grep",
        "-g",
        help="Filter log lines containing this string",
    ),
    context: int = typer.Option(
        0,
        "--context",
        "-C",
        help="Number of context lines around matches",
    ),
) -> None:
    console.print(f"[yellow]Fetching log for run #{run_id}...[/yellow]")
    log = get_run_log(run_id)

    if not log:
        console.print("[red]Could not fetch log or log is empty.[/red]")
        return

    if grep:
        lines = log.split("\n")
        matching_indices = [i for i, line in enumerate(lines) if grep.lower() in line.lower()]

        if not matching_indices:
            console.print(f"[yellow]No matches found for '{grep}'[/yellow]")
            return

        console.print(f"[green]Found {len(matching_indices)} matches:[/green]\n")

        printed_ranges: set[int] = set()
        for idx in matching_indices:
            start = max(0, idx - context)
            end = min(len(lines), idx + context + 1)

            # Skip if already printed
            if any(start <= p <= end for p in printed_ranges):
                continue

            for i in range(start, end):
                printed_ranges.add(i)
                prefix = ">>> " if i == idx else "    "
                style = "bold yellow" if i == idx else "dim"
                console.print(f"[{style}]{prefix}{lines[i]}[/{style}]")

            console.print("---")
    else:
        print(log)
