from __future__ import annotations

from dataclasses import dataclass

from symx.admin.actions import AdminStore, ApplyBatchRequest, IpswTarget, OtaTarget, preview_action, target_label
from symx.ipsw.model import IpswArtifactDb, IpswSource
from symx.model import ArtifactProcessingState
from symx.ota.model import OtaArtifact


@dataclass(frozen=True)
class ValidationIssue:
    target: str
    reason: str


class AdminApplyValidationError(RuntimeError):
    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__(format_validation_issues(issues))


def apply_request_to_ipsw_db(ipsw_db: IpswArtifactDb, request: ApplyBatchRequest) -> int:
    if request.store != AdminStore.IPSW:
        raise ValueError("IPSW apply requested with a non-IPSW batch")

    resolved_targets: list[tuple[IpswSource, ArtifactProcessingState]] = []
    issues: list[ValidationIssue] = []
    for target in request.targets:
        if not isinstance(target, IpswTarget):
            issues.append(ValidationIssue(target=target_label(target), reason="unexpected target type for ipsw batch"))
            continue

        artifact = ipsw_db.artifacts.get(target.artifact_key)
        if artifact is None:
            issues.append(ValidationIssue(target=target_label(target), reason="artifact not found in meta-data"))
            continue

        source = _find_ipsw_source(artifact.sources, target.link)
        if source is None:
            issues.append(ValidationIssue(target=target_label(target), reason="source link not found in artifact"))
            continue

        preview = preview_action(
            AdminStore.IPSW,
            request.action,
            source.processing_state,
            has_required_path=source.mirror_path is not None,
        )
        if not preview.allowed or preview.resulting_state is None:
            issues.append(ValidationIssue(target=target_label(target), reason=preview.note))
            continue
        resolved_targets.append((source, preview.resulting_state))

    if issues:
        raise AdminApplyValidationError(tuple(issues))

    for source, resulting_state in resolved_targets:
        source.processing_state = resulting_state
        source.update_last_run()

    return len(resolved_targets)


def apply_request_to_ota_meta(ota_meta: dict[str, OtaArtifact], request: ApplyBatchRequest) -> int:
    if request.store != AdminStore.OTA:
        raise ValueError("OTA apply requested with a non-OTA batch")

    resolved_targets: list[tuple[OtaArtifact, ArtifactProcessingState]] = []
    issues: list[ValidationIssue] = []
    for target in request.targets:
        if not isinstance(target, OtaTarget):
            issues.append(ValidationIssue(target=target_label(target), reason="unexpected target type for ota batch"))
            continue

        artifact = ota_meta.get(target.ota_key)
        if artifact is None:
            issues.append(ValidationIssue(target=target_label(target), reason="ota artifact not found in meta-data"))
            continue

        preview = preview_action(
            AdminStore.OTA,
            request.action,
            artifact.processing_state,
            has_required_path=artifact.download_path is not None,
        )
        if not preview.allowed or preview.resulting_state is None:
            issues.append(ValidationIssue(target=target_label(target), reason=preview.note))
            continue
        resolved_targets.append((artifact, preview.resulting_state))

    if issues:
        raise AdminApplyValidationError(tuple(issues))

    for artifact, resulting_state in resolved_targets:
        artifact.processing_state = resulting_state
        artifact.update_last_run()

    return len(resolved_targets)


def format_validation_issues(issues: tuple[ValidationIssue, ...]) -> str:
    return "; ".join(f"{issue.target}: {issue.reason}" for issue in issues)


def _find_ipsw_source(sources: list[IpswSource], link: str) -> IpswSource | None:
    for source in sources:
        if str(source.link) == link:
            return source
    return None
