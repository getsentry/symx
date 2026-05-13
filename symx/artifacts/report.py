"""Parity reports for normalized artifacts generated from legacy metadata."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from symx.admin.meta_json import parse_ota_meta_json
from symx.artifacts.convert import convert_ipsw_db, convert_ota_meta
from symx.artifacts.model import ArtifactBundle, ArtifactKind
from symx.ipsw.model import IpswArtifact, IpswArtifactDb
from symx.ipsw.storage.gcs import extract_filter as ipsw_extract_filter
from symx.ipsw.storage.gcs import mirror_filter as ipsw_mirror_filter
from symx.model import ArtifactProcessingState
from symx.ota.model import OtaMetaData, parse_version_tuple


class ArtifactReportError(RuntimeError):
    pass


class WorklistParity(BaseModel):
    legacy_count: int
    v2_count: int
    matches: bool
    first_legacy_uids: list[str] = Field(default_factory=list)
    first_v2_uids: list[str] = Field(default_factory=list)


class ArtifactParityReport(BaseModel):
    schema_version: int = 1
    generated_at: str
    total_artifacts: int
    totals_by_kind: dict[str, int]
    state_counts_by_kind: dict[str, dict[str, int]]
    ipsw_source_count: int
    ota_artifact_count: int
    worklists: dict[str, WorklistParity]
    mismatches: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches


def load_ipsw_meta(path: Path) -> IpswArtifactDb:
    return IpswArtifactDb.model_validate_json(path.read_text())


def load_ota_meta(path: Path) -> OtaMetaData:
    return parse_ota_meta_json(path.read_text(), ArtifactReportError)


def build_parity_report(ipsw_db: IpswArtifactDb, ota_meta: OtaMetaData) -> ArtifactParityReport:
    bundles = [*convert_ipsw_db(ipsw_db), *convert_ota_meta(ota_meta)]
    mismatches: list[str] = []

    ipsw_source_count = sum(len(artifact.sources) for artifact in ipsw_db.artifacts.values())
    ota_artifact_count = len(ota_meta)
    if len(bundles) != ipsw_source_count + ota_artifact_count:
        mismatches.append(f"artifact count mismatch: v2={len(bundles)} legacy={ipsw_source_count + ota_artifact_count}")

    artifact_uids = [bundle.artifact.artifact_uid for bundle in bundles]
    duplicate_uids = sorted(uid for uid, count in Counter(artifact_uids).items() if count > 1)
    if duplicate_uids:
        mismatches.append(f"duplicate artifact UIDs: {', '.join(duplicate_uids[:10])}")

    for name, parity in _worklist_parity(ipsw_db, ota_meta, bundles).items():
        if not parity.matches:
            mismatches.append(f"worklist mismatch: {name} legacy={parity.legacy_count} v2={parity.v2_count}")

    return ArtifactParityReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        total_artifacts=len(bundles),
        totals_by_kind=_totals_by_kind(bundles),
        state_counts_by_kind=_state_counts_by_kind(bundles),
        ipsw_source_count=ipsw_source_count,
        ota_artifact_count=ota_artifact_count,
        worklists=_worklist_parity(ipsw_db, ota_meta, bundles),
        mismatches=mismatches,
    )


def build_parity_report_from_files(ipsw_meta_path: Path, ota_meta_path: Path) -> ArtifactParityReport:
    return build_parity_report(load_ipsw_meta(ipsw_meta_path), load_ota_meta(ota_meta_path))


def write_parity_report_json(report: ArtifactParityReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.model_dump_json(indent=2) + "\n")


def _totals_by_kind(bundles: Iterable[ArtifactBundle]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for bundle in bundles:
        counts[bundle.artifact.kind.value] += 1
    return dict(sorted(counts.items()))


def _state_counts_by_kind(bundles: Iterable[ArtifactBundle]) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = {}
    for bundle in bundles:
        kind = bundle.artifact.kind.value
        if kind not in counts:
            counts[kind] = Counter()
        counts[kind][bundle.artifact.processing_state.value] += 1
    return {kind: dict(sorted(state_counts.items())) for kind, state_counts in sorted(counts.items())}


def _worklist_parity(
    ipsw_db: IpswArtifactDb,
    ota_meta: OtaMetaData,
    bundles: list[ArtifactBundle],
) -> dict[str, WorklistParity]:
    return {
        "ipsw_mirror": _parity(_legacy_ipsw_mirror_worklist(ipsw_db), _v2_ipsw_worklist(bundles, "mirror")),
        "ipsw_extract": _parity(_legacy_ipsw_extract_worklist(ipsw_db), _v2_ipsw_worklist(bundles, "extract")),
        "ota_mirror": _parity(_legacy_ota_mirror_worklist(ota_meta), _v2_ota_mirror_worklist(bundles)),
        "ota_extract": _parity(_legacy_ota_extract_worklist(ota_meta), _v2_ota_extract_worklist(bundles)),
    }


def _parity(legacy_uids: list[str], v2_uids: list[str]) -> WorklistParity:
    return WorklistParity(
        legacy_count=len(legacy_uids),
        v2_count=len(v2_uids),
        matches=legacy_uids == v2_uids,
        first_legacy_uids=legacy_uids[:20],
        first_v2_uids=v2_uids[:20],
    )


def _legacy_ipsw_mirror_worklist(ipsw_db: IpswArtifactDb) -> list[str]:
    artifacts = _sort_ipsw_artifacts_by_release_desc(ipsw_mirror_filter(ipsw_db.artifacts.values()))
    return [
        bundle.artifact.artifact_uid
        for artifact in artifacts
        for bundle in convert_ipsw_db(IpswArtifactDb(artifacts={artifact.key: artifact}))
        if bundle.artifact.processing_state == ArtifactProcessingState.INDEXED
    ]


def _legacy_ipsw_extract_worklist(ipsw_db: IpswArtifactDb) -> list[str]:
    artifacts = _sort_ipsw_artifacts_by_release_desc(ipsw_extract_filter(ipsw_db.artifacts.values()))
    return [
        bundle.artifact.artifact_uid
        for artifact in artifacts
        for bundle in convert_ipsw_db(IpswArtifactDb(artifacts={artifact.key: artifact}))
        if bundle.artifact.processing_state == ArtifactProcessingState.MIRRORED
    ]


def _v2_ipsw_worklist(bundles: list[ArtifactBundle], mode: str) -> list[str]:
    state = ArtifactProcessingState.INDEXED if mode == "mirror" else ArtifactProcessingState.MIRRORED
    filtered = [
        bundle
        for bundle in bundles
        if bundle.artifact.kind == ArtifactKind.IPSW and bundle.artifact.processing_state == state
    ]
    if mode == "mirror":
        current_year = date.today().year
        filtered = [
            bundle
            for bundle in filtered
            if bundle.artifact.released_at is not None and bundle.artifact.released_at.year >= current_year - 1
        ]
    return [bundle.artifact.artifact_uid for bundle in sorted(filtered, key=_v2_ipsw_sort_key, reverse=True)]


def _legacy_ota_mirror_worklist(ota_meta: OtaMetaData) -> list[str]:
    return [
        bundle.artifact.artifact_uid
        for key, ota in ota_meta.items()
        if ota.is_indexed()
        for bundle in [convert_ota_meta({key: ota})[0]]
    ]


def _v2_ota_mirror_worklist(bundles: list[ArtifactBundle]) -> list[str]:
    return [
        bundle.artifact.artifact_uid
        for bundle in bundles
        if bundle.artifact.kind == ArtifactKind.OTA
        and bundle.artifact.processing_state == ArtifactProcessingState.INDEXED
    ]


def _legacy_ota_extract_worklist(ota_meta: OtaMetaData) -> list[str]:
    mirrored = [(key, ota) for key, ota in ota_meta.items() if ota.is_mirrored()]
    mirrored.sort(key=lambda item: parse_version_tuple(item[1].version), reverse=True)
    return [convert_ota_meta({key: ota})[0].artifact.artifact_uid for key, ota in mirrored]


def _v2_ota_extract_worklist(bundles: list[ArtifactBundle]) -> list[str]:
    indexed = list(enumerate(bundles))
    mirrored = [
        (idx, bundle)
        for idx, bundle in indexed
        if bundle.artifact.kind == ArtifactKind.OTA
        and bundle.artifact.processing_state == ArtifactProcessingState.MIRRORED
    ]
    mirrored.sort(key=lambda item: parse_version_tuple(item[1].artifact.version), reverse=True)
    return [bundle.artifact.artifact_uid for _, bundle in mirrored]


def _sort_ipsw_artifacts_by_release_desc(artifacts: Iterable[IpswArtifact]) -> list[IpswArtifact]:
    return sorted(artifacts, key=lambda artifact: artifact.released or date.min, reverse=True)


def _v2_ipsw_sort_key(bundle: ArtifactBundle) -> date:
    return bundle.artifact.released_at or date.min


def report_to_json(report: ArtifactParityReport) -> str:
    return json.dumps(json.loads(report.model_dump_json()), indent=2, sort_keys=True) + "\n"
