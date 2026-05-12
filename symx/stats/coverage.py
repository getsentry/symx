from __future__ import annotations

import html
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache, cmp_to_key
from importlib.resources import files
from pathlib import Path

from symx.admin.db import SnapshotInfo, active_snapshot_paths, load_snapshot_info
from symx.model import ArtifactProcessingState

COVERAGE_STATE = ArtifactProcessingState.SYMBOLS_EXTRACTED
_VERSION_PARTS_RE = re.compile(r"\d+")
_VERSION_PREFIX_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*(.*)$", re.IGNORECASE)
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+}}")


class CoverageReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class IpswCoverageRow:
    platform: str
    version: str
    count: int


@dataclass(frozen=True)
class OtaCoverageRow:
    platform: str
    version: str
    count: int


CoverageTableRow = IpswCoverageRow | OtaCoverageRow


@dataclass(frozen=True)
class ParsedCoverageVersion:
    normalized: str
    base_parts: tuple[int, ...]
    major: int | None
    minor: int | None
    patch: int | None
    stage_rank: int
    stage_number: int


@dataclass(frozen=True)
class CoverageReport:
    generated_at: str
    snapshot_info: SnapshotInfo
    ipsw_rows: list[IpswCoverageRow]
    ota_rows: list[OtaCoverageRow]

    @property
    def ipsw_total_count(self) -> int:
        return sum(row.count for row in self.ipsw_rows)

    @property
    def ota_total_count(self) -> int:
        return sum(row.count for row in self.ota_rows)


def resolve_snapshot_db(cache_dir: Path, db_path: Path | None = None) -> Path:
    if db_path is not None:
        if not db_path.exists():
            raise CoverageReportError(f"Snapshot DB does not exist: {db_path}")
        return db_path

    active_paths = active_snapshot_paths(cache_dir)
    if active_paths is None:
        raise CoverageReportError(f"No active snapshot DB found under {cache_dir}")
    return active_paths.db_path


def build_coverage_report(db_path: Path) -> CoverageReport:
    snapshot_info = load_snapshot_info(db_path)
    if snapshot_info is None:
        raise CoverageReportError(f"Snapshot info is missing from {db_path}")

    conn = _connect_read_only(db_path)
    try:
        ipsw_rows = _load_ipsw_coverage_rows(conn)
        ota_rows = _load_ota_coverage_rows(conn)
    finally:
        conn.close()

    return CoverageReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        snapshot_info=snapshot_info,
        ipsw_rows=ipsw_rows,
        ota_rows=ota_rows,
    )


def write_coverage_html(output_path: Path, report: CoverageReport) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_coverage_html(report))


def render_coverage_html(report: CoverageReport) -> str:
    snapshot = report.snapshot_info
    return _render_template(
        "coverage.html",
        {
            "GENERATED_AT": _escape(report.generated_at),
            "SNAPSHOT_ID": _escape(snapshot.snapshot_id),
            "SNAPSHOT_CREATED_AT": _escape(snapshot.created_at),
            "IPSW_GENERATION": str(snapshot.ipsw_generation),
            "OTA_GENERATION": str(snapshot.ota_generation),
            "WORKFLOW_RUN_HTML": _workflow_run_html(snapshot),
            "IPSW_SECTION": _render_section(
                "ipsw",
                "IPSW coverage",
                report.ipsw_rows,
                report.ipsw_total_count,
                nav_html='    <p class="section-links"><a href="#top">Back to top</a> · <a href="#ota-section">Jump to OTA coverage</a></p>\n',
            ),
            "OTA_SECTION": _render_section(
                "ota",
                "OTA coverage",
                report.ota_rows,
                report.ota_total_count,
                nav_html='    <p class="section-links"><a href="#top">Back to top</a> · <a href="#ipsw-section">Jump to IPSW coverage</a></p>\n',
            ),
            "COVERAGE_DATA_JSON": _json_script_payload(
                {
                    "ipswRows": _coverage_row_payloads(report.ipsw_rows),
                    "otaRows": _coverage_row_payloads(report.ota_rows),
                }
            ),
            "COVERAGE_SCRIPT": _load_template_text("coverage.js").rstrip(),
        },
    )


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_ipsw_coverage_rows(conn: sqlite3.Connection) -> list[IpswCoverageRow]:
    rows = conn.execute(
        """
        SELECT
            a.platform,
            a.version,
            COUNT(*) AS count
        FROM ipsw_sources AS s
        JOIN ipsw_artifacts AS a ON a.artifact_key = s.artifact_key
        WHERE s.processing_state = ?
        GROUP BY a.platform, a.version
        """,
        (COVERAGE_STATE.value,),
    ).fetchall()

    return sorted(
        [
            IpswCoverageRow(
                platform=str(row["platform"]),
                version=str(row["version"]),
                count=int(row["count"]),
            )
            for row in rows
        ],
        key=cmp_to_key(_compare_coverage_rows),
    )


def _load_ota_coverage_rows(conn: sqlite3.Connection) -> list[OtaCoverageRow]:
    rows = conn.execute(
        """
        SELECT
            platform,
            version,
            COUNT(*) AS count
        FROM ota_artifacts
        WHERE processing_state = ?
        GROUP BY platform, version
        """,
        (COVERAGE_STATE.value,),
    ).fetchall()

    return sorted(
        [
            OtaCoverageRow(
                platform=str(row["platform"]),
                version=str(row["version"]),
                count=int(row["count"]),
            )
            for row in rows
        ],
        key=cmp_to_key(_compare_coverage_rows),
    )


def _compare_coverage_rows(left: CoverageTableRow, right: CoverageTableRow) -> int:
    platform_cmp = _compare_platform(left.platform, right.platform)
    if platform_cmp != 0:
        return platform_cmp

    left_version = _parse_coverage_version(left.version)
    right_version = _parse_coverage_version(right.version)

    base_parts_cmp = _compare_int_tuple_desc(left_version.base_parts, right_version.base_parts)
    if base_parts_cmp != 0:
        return base_parts_cmp

    stage_cmp = _compare_int_desc(left_version.stage_rank, right_version.stage_rank)
    if stage_cmp != 0:
        return stage_cmp

    stage_number_cmp = _compare_int_desc(left_version.stage_number, right_version.stage_number)
    if stage_number_cmp != 0:
        return stage_number_cmp

    return _compare_raw_version_asc(left_version.normalized, right_version.normalized)


def _compare_platform(left: str, right: str) -> int:
    left_key = left.casefold()
    right_key = right.casefold()
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def _compare_raw_version_asc(left: str, right: str) -> int:
    left_key = left.casefold()
    right_key = right.casefold()
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


@cache
def _parse_coverage_version(version: str) -> ParsedCoverageVersion:
    normalized = _display_version(version).strip()
    match = _VERSION_PREFIX_RE.match(normalized)
    if match is None:
        base_parts: tuple[int, ...] = ()
        suffix = normalized.casefold()
    else:
        base_parts = tuple(int(part) for part in match.group(1).split("."))
        suffix = match.group(2).strip().casefold()

    return ParsedCoverageVersion(
        normalized=normalized,
        base_parts=base_parts,
        major=base_parts[0] if len(base_parts) >= 1 else None,
        minor=base_parts[1] if len(base_parts) >= 2 else None,
        patch=base_parts[2] if len(base_parts) >= 3 else None,
        stage_rank=_version_stage_rank(suffix),
        stage_number=_version_stage_number(suffix),
    )


def _version_stage_rank(suffix: str) -> int:
    if suffix == "":
        return 3
    if "release candidate" in suffix or re.search(r"\brc\b", suffix) is not None:
        return 2
    if "beta" in suffix:
        return 1
    return 0


def _version_stage_number(suffix: str) -> int:
    match = _VERSION_PARTS_RE.search(suffix)
    if match is None:
        return 0
    return int(match.group(0))


def _compare_int_tuple_desc(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    max_length = max(len(left), len(right))
    for index in range(max_length):
        left_value = left[index] if index < len(left) else -1
        right_value = right[index] if index < len(right) else -1
        if left_value > right_value:
            return -1
        if left_value < right_value:
            return 1
    return 0


def _compare_int_desc(left: int, right: int) -> int:
    if left > right:
        return -1
    if left < right:
        return 1
    return 0


def _workflow_run_html(snapshot: SnapshotInfo) -> str:
    if snapshot.workflow_run_url is None:
        return ""

    label = f"#{snapshot.workflow_run_id}" if snapshot.workflow_run_id is not None else "workflow run"
    return f'    <li>workflow run: <a href="{_escape(snapshot.workflow_run_url)}">{_escape(label)}</a></li>'


def _render_section(
    section_id: str,
    title: str,
    rows: Iterable[CoverageTableRow],
    total_count: int,
    *,
    nav_html: str,
) -> str:
    return "".join(
        [
            f'  <section id="{section_id}-section">\n',
            f"    <h2>{_escape(title)}</h2>\n",
            nav_html,
            _render_filters(section_id),
            f'    <p class="summary" id="{section_id}-summary">Count: <strong>{total_count}</strong></p>\n',
            "    <table>\n",
            "      <thead><tr><th>platform</th><th>version</th><th>count</th></tr></thead>\n",
            f'      <tbody id="{section_id}-tbody">\n{_render_table_rows(rows)}      </tbody>\n',
            "    </table>\n",
            "  </section>\n",
        ]
    )


def _render_filters(section_id: str) -> str:
    return "".join(
        [
            '    <div class="filters" id="',
            section_id,
            '-filters">\n',
            _render_filter_control(section_id, "platform", "platform"),
            _render_filter_control(section_id, "major", "major"),
            _render_filter_control(section_id, "minor", "minor"),
            _render_filter_control(section_id, "patch", "patch"),
            f'      <div class="filter-actions"><button type="button" id="{section_id}-reset" disabled>Reset filters</button></div>\n',
            "    </div>\n",
        ]
    )


def _render_filter_control(section_id: str, name: str, label: str) -> str:
    return (
        f'      <div class="filter-control" id="{section_id}-{name}-control">'
        f'<label for="{section_id}-{name}">{_escape(label)}</label>'
        f'<select id="{section_id}-{name}" disabled></select>'
        "</div>\n"
    )


def _render_table_rows(rows: Iterable[CoverageTableRow]) -> str:
    materialized_rows = list(rows)
    if not materialized_rows:
        return '        <tr><td colspan="3">No rows.</td></tr>\n'

    return "".join(
        "".join(
            [
                "        <tr>",
                f"<td>{_escape(row.platform)}</td>",
                f"<td>{_escape(_display_version(row.version))}</td>",
                f'<td class="count">{row.count}</td>',
                "</tr>\n",
            ]
        )
        for row in materialized_rows
    )


def _coverage_row_payloads(rows: Iterable[CoverageTableRow]) -> list[dict[str, object]]:
    return [_coverage_row_payload(row) for row in rows]


def _coverage_row_payload(row: CoverageTableRow) -> dict[str, object]:
    parsed_version = _parse_coverage_version(row.version)
    return {
        "platform": row.platform,
        "version": row.version,
        "versionDisplay": _display_version(row.version),
        "baseParts": list(parsed_version.base_parts),
        "major": parsed_version.major,
        "minor": parsed_version.minor,
        "patch": parsed_version.patch,
        "stageRank": parsed_version.stage_rank,
        "stageNumber": parsed_version.stage_number,
        "count": row.count,
    }


@cache
def _load_template_text(template_name: str) -> str:
    return files("symx.stats.templates").joinpath(template_name).read_text(encoding="utf-8")


def _render_template(template_name: str, replacements: dict[str, str]) -> str:
    rendered = _load_template_text(template_name)
    for key, value in replacements.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)

    unmatched = _TEMPLATE_PLACEHOLDER_RE.findall(rendered)
    if unmatched:
        raise CoverageReportError(
            f"Template {template_name} still has unreplaced placeholders: {', '.join(sorted(unmatched))}"
        )

    return rendered


def _json_script_payload(payload: object) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")


def _display_version(version: str) -> str:
    return version.replace("_", " ")


def _escape(value: str) -> str:
    return html.escape(value, quote=True)
