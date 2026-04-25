import json
from pathlib import Path
from unittest.mock import patch

from symx.admin.github import (
    GithubRunInfo,
    ensure_github_run_infos,
    fetch_github_run_info,
    format_github_run_time,
    read_github_run_cache,
    write_github_run_cache,
)


def test_github_run_cache_round_trip(tmp_path: Path) -> None:
    cache = {
        123: GithubRunInfo(
            run_id=123,
            started_at="2024-09-03T10:00:00Z",
            updated_at="2024-09-03T12:34:56Z",
            url="https://example.invalid/run/123",
            status="completed",
            conclusion="failure",
            display_title="OTA extract",
        )
    }

    write_github_run_cache(tmp_path, cache)

    loaded = read_github_run_cache(tmp_path)
    assert loaded == cache


def test_format_github_run_time_prefers_timestamp() -> None:
    info = GithubRunInfo(run_id=123, updated_at="2024-09-03T12:34:56Z")

    assert format_github_run_time(123, info) == "2024-09-03 12:34Z"
    assert format_github_run_time(123, None) == "#123"


def test_fetch_github_run_info_uses_gh_cli() -> None:
    payload = {
        "databaseId": 123,
        "startedAt": "2024-09-03T10:00:00Z",
        "updatedAt": "2024-09-03T12:34:56Z",
        "url": "https://example.invalid/run/123",
        "status": "completed",
        "conclusion": "failure",
        "displayTitle": "Extract OTA symbols",
    }

    with patch("symx.admin.github.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = json.dumps(payload)
        mock_run.return_value.stderr = ""

        info = fetch_github_run_info(123)

    assert info == GithubRunInfo(
        run_id=123,
        started_at="2024-09-03T10:00:00Z",
        updated_at="2024-09-03T12:34:56Z",
        url="https://example.invalid/run/123",
        status="completed",
        conclusion="failure",
        display_title="Extract OTA symbols",
    )


def test_ensure_github_run_infos_uses_cache_for_existing_runs(tmp_path: Path) -> None:
    cached = GithubRunInfo(run_id=123, updated_at="2024-09-03T12:34:56Z")
    fetched = GithubRunInfo(run_id=456, updated_at="2024-09-04T12:34:56Z")
    write_github_run_cache(tmp_path, {123: cached})

    with patch("symx.admin.github.fetch_github_run_info", return_value=fetched) as fetch_run_info:
        result = ensure_github_run_infos(tmp_path, [123, 456])

    fetch_run_info.assert_called_once_with(456)
    assert result[123] == cached
    assert result[456] == fetched
