from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TypedDict, cast

GITHUB_RUN_CACHE_FILE_NAME: Final[str] = "github_runs.json"


class GithubRunLookupError(RuntimeError):
    pass


class GithubRunPayload(TypedDict):
    databaseId: int
    startedAt: str | None
    updatedAt: str | None
    url: str | None
    status: str | None
    conclusion: str | None
    displayTitle: str | None


@dataclass(frozen=True)
class GithubRunInfo:
    run_id: int
    started_at: str | None = None
    updated_at: str | None = None
    url: str | None = None
    status: str | None = None
    conclusion: str | None = None
    display_title: str | None = None

    @property
    def best_timestamp(self) -> str | None:
        return self.updated_at or self.started_at


StatusCallback = Callable[[str], None]


def github_run_cache_path(cache_dir: Path) -> Path:
    return cache_dir / GITHUB_RUN_CACHE_FILE_NAME


def read_github_run_cache(cache_dir: Path) -> dict[int, GithubRunInfo]:
    path = github_run_cache_path(cache_dir)
    if not path.exists():
        return {}

    raw_payload: object = json.loads(path.read_text())
    if not isinstance(raw_payload, dict):
        return {}

    payload = cast(dict[object, object], raw_payload)
    result: dict[int, GithubRunInfo] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        try:
            run_id = int(key)
        except ValueError:
            continue
        value_dict = cast(dict[object, object], value)
        result[run_id] = GithubRunInfo(
            run_id=run_id,
            started_at=_optional_str(value_dict.get("started_at")),
            updated_at=_optional_str(value_dict.get("updated_at")),
            url=_optional_str(value_dict.get("url")),
            status=_optional_str(value_dict.get("status")),
            conclusion=_optional_str(value_dict.get("conclusion")),
            display_title=_optional_str(value_dict.get("display_title")),
        )
    return result


def write_github_run_cache(cache_dir: Path, run_infos: dict[int, GithubRunInfo]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {str(run_id): asdict(info) for run_id, info in sorted(run_infos.items())}
    github_run_cache_path(cache_dir).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def ensure_github_run_infos(
    cache_dir: Path,
    run_ids: Iterable[int],
    status_callback: StatusCallback | None = None,
) -> dict[int, GithubRunInfo]:
    cache = read_github_run_cache(cache_dir)
    missing_run_ids = sorted({run_id for run_id in run_ids if run_id not in cache and run_id > 0})

    for run_id in missing_run_ids:
        try:
            info = fetch_github_run_info(run_id)
        except GithubRunLookupError as exc:
            if status_callback is not None:
                status_callback(f"Failed to resolve GitHub run #{run_id}: {exc}")
            continue
        cache[run_id] = info

    if missing_run_ids:
        write_github_run_cache(cache_dir, cache)

    return cache


def fetch_github_run_info(run_id: int) -> GithubRunInfo:
    result = subprocess.run(
        [
            "gh",
            "run",
            "view",
            str(run_id),
            "--json",
            "databaseId,startedAt,updatedAt,url,status,conclusion,displayTitle",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise GithubRunLookupError(stderr)

    raw_payload: object = json.loads(result.stdout)
    if not isinstance(raw_payload, dict):
        raise GithubRunLookupError("Unexpected gh run payload")

    payload = cast(GithubRunPayload, raw_payload)
    return GithubRunInfo(
        run_id=int(payload["databaseId"]),
        started_at=payload["startedAt"],
        updated_at=payload["updatedAt"],
        url=payload["url"],
        status=payload["status"],
        conclusion=payload["conclusion"],
        display_title=payload["displayTitle"],
    )


def format_github_run_time(run_id: int, run_info: GithubRunInfo | None) -> str:
    if run_info is None or run_info.best_timestamp is None:
        return f"#{run_id}"
    return format_iso_timestamp(run_info.best_timestamp)


def format_iso_timestamp(timestamp: str) -> str:
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
