"""Shared helpers for formatting subprocess and filesystem diagnostics."""

import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import cast

MAX_SUBPROCESS_OUTPUT_CHARS = 4_000
DEFAULT_DIRECTORY_SAMPLE_ENTRIES = 20


def decode_subprocess_output(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def truncate_text(output: str | bytes | None, max_chars: int = MAX_SUBPROCESS_OUTPUT_CHARS) -> str | None:
    text = decode_subprocess_output(output).strip()
    if not text:
        return None

    if len(text) > max_chars:
        omitted = len(text) - max_chars
        text = f"{text[:max_chars]}\n... [truncated {omitted} chars]"
    return text


def format_subprocess_output(
    label: str,
    output: str | bytes | None,
    max_chars: int = MAX_SUBPROCESS_OUTPUT_CHARS,
    indent: str = "    ",
) -> str:
    text = truncate_text(output, max_chars=max_chars)
    if text is None:
        return f"  {label}: <empty>"

    indented = "\n".join(f"{indent}{line}" for line in text.splitlines())
    return f"  {label}:\n{indented}"


def subprocess_result_data(
    result: subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str] | None,
) -> dict[str, object]:
    if result is None:
        return {"attempted": False}

    return {
        "attempted": True,
        "returncode": result.returncode,
        "stdout": truncate_text(result.stdout),
        "stderr": truncate_text(result.stderr),
    }


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


def directory_data(directory: Path, max_entries: int = DEFAULT_DIRECTORY_SAMPLE_ENTRIES) -> dict[str, object]:
    data: dict[str, object] = {
        "path": str(directory),
        "exists": directory.exists(),
        "is_dir": directory.is_dir(),
    }
    if not directory.exists() or not directory.is_dir():
        return data

    sample_entries: list[str] = []
    truncated = False
    for path in directory.rglob("*"):
        suffix = "/" if path.is_dir() else ""
        sample_entries.append(f"{path.relative_to(directory)}{suffix}")
        if len(sample_entries) >= max_entries:
            truncated = True
            break

    data["sample_entries"] = sample_entries
    data["sample_truncated"] = truncated
    return data


def describe_directory(directory: Path, max_entries: int = DEFAULT_DIRECTORY_SAMPLE_ENTRIES, indent: str = "  ") -> str:
    data = directory_data(directory, max_entries=max_entries)
    if not data["exists"]:
        return f"directory {directory}: <missing>"
    if not data["is_dir"]:
        return f"directory {directory}: <not a directory>"

    sample_entries_obj = data.get("sample_entries")
    if not isinstance(sample_entries_obj, list) or not sample_entries_obj:
        return f"directory {directory}: (empty)"

    sample_entries_raw = cast(list[object], sample_entries_obj)
    sample_entries = [str(entry) for entry in sample_entries_raw]
    lines = [f"directory {directory} sample entries:"]
    lines.extend(f"{indent}{entry}" for entry in sample_entries)
    if data.get("sample_truncated"):
        lines.append(f"{indent}... showing first {max_entries} entries")
    return "\n".join(lines)
