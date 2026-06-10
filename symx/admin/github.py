from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

GITHUB_RUN_CACHE_FILE_NAME: Final[str] = "github_runs.json"


class GithubRunLookupError(RuntimeError):
    pass


class _GithubPayloadModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _GithubRunPayload(_GithubPayloadModel):
    databaseId: int
    startedAt: str | None = None
    updatedAt: str | None = None
    url: str | None = None
    status: str | None = None
    conclusion: str | None = None
    displayTitle: str | None = None


class _GithubRunCachePayload(_GithubPayloadModel):
    run_id: int
    started_at: str | None = None
    updated_at: str | None = None
    url: str | None = None
    status: str | None = None
    conclusion: str | None = None
    display_title: str | None = None


_GITHUB_RUN_CACHE = TypeAdapter(dict[str, _GithubRunCachePayload])


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

    try:
        payload = _GITHUB_RUN_CACHE.validate_json(path.read_text())
    except ValidationError:
        return {}

    result: dict[int, GithubRunInfo] = {}
    for key, value in payload.items():
        try:
            run_id = int(key)
        except ValueError:
            continue
        result[run_id] = GithubRunInfo(
            run_id=run_id,
            started_at=value.started_at,
            updated_at=value.updated_at,
            url=value.url,
            status=value.status,
            conclusion=value.conclusion,
            display_title=value.display_title,
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

    try:
        payload = _GithubRunPayload.model_validate_json(result.stdout)
    except ValidationError as error:
        raise GithubRunLookupError("Unexpected gh run payload") from error

    return GithubRunInfo(
        run_id=payload.databaseId,
        started_at=payload.startedAt,
        updated_at=payload.updatedAt,
        url=payload.url,
        status=payload.status,
        conclusion=payload.conclusion,
        display_title=payload.displayTitle,
    )


def format_github_run_time(run_id: int, run_info: GithubRunInfo | None) -> str:
    if run_info is None or run_info.best_timestamp is None:
        return f"#{run_id}"
    return format_iso_timestamp(run_info.best_timestamp)


def format_iso_timestamp(timestamp: str) -> str:
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
