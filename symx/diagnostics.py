"""Shared helpers for formatting subprocess and filesystem diagnostics."""

import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

MAX_SUBPROCESS_OUTPUT_CHARS = 4_000
DEFAULT_DIRECTORY_SAMPLE_ENTRIES = 20


def decode_subprocess_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def format_subprocess_output(
    label: str,
    output: str | bytes | None,
    max_chars: int = MAX_SUBPROCESS_OUTPUT_CHARS,
    indent: str = "    ",
) -> str:
    text = decode_subprocess_output(output).strip()
    if not text:
        return f"  {label}: <empty>"

    if len(text) > max_chars:
        omitted = len(text) - max_chars
        text = f"{text[:max_chars]}\n... [truncated {omitted} chars]"

    indented = "\n".join(f"{indent}{line}" for line in text.splitlines())
    return f"  {label}:\n{indented}"


def format_subprocess_result(
    label: str,
    result: subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str] | None,
) -> str:
    if result is None:
        return f"{label}: not attempted"

    return "\n".join(
        [
            f"{label}: exit={result.returncode}",
            format_subprocess_output("stdout", result.stdout),
            format_subprocess_output("stderr", result.stderr),
        ]
    )


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def describe_directory(directory: Path, max_entries: int = DEFAULT_DIRECTORY_SAMPLE_ENTRIES, indent: str = "  ") -> str:
    if not directory.exists():
        return f"directory {directory}: <missing>"
    if not directory.is_dir():
        return f"directory {directory}: <not a directory>"

    sample_entries: list[str] = []
    for path in directory.rglob("*"):
        suffix = "/" if path.is_dir() else ""
        sample_entries.append(f"{path.relative_to(directory)}{suffix}")
        if len(sample_entries) >= max_entries:
            break

    if not sample_entries:
        return f"directory {directory}: (empty)"

    lines = [f"directory {directory} sample entries:"]
    lines.extend(f"{indent}{entry}" for entry in sample_entries)
    if len(sample_entries) >= max_entries:
        lines.append(f"{indent}... showing first {max_entries} entries")
    return "\n".join(lines)
