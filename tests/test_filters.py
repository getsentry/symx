"""
Tests for artifact filtering logic used by mirror/extract workflows.

These filters determine which artifacts get processed in each workflow stage.
"""

from datetime import date, timedelta

from pydantic import HttpUrl

from symx._common import ArtifactProcessingState
from symx._ipsw.common import IpswArtifact, IpswPlatform, IpswReleaseStatus, IpswSource
from symx._ipsw.storage.gcs import mirror_filter, extract_filter


def make_source(state: ArtifactProcessingState = ArtifactProcessingState.INDEXED) -> IpswSource:
    return IpswSource(
        devices=["iPhone14,7"],
        link=HttpUrl("https://example.com/test.ipsw"),
        processing_state=state,
    )


def make_artifact(
    released: date | None = None,
    sources: list[IpswSource] | None = None,
) -> IpswArtifact:
    return IpswArtifact(
        platform=IpswPlatform.IOS,
        version="17.0",
        build="21A100",
        released=released,
        release_status=IpswReleaseStatus.RELEASE,
        sources=sources or [make_source()],
    )


# --- mirror_filter tests ---


def test_mirror_filter_includes_indexed_artifact_from_current_year() -> None:
    artifact = make_artifact(
        released=date.today(),
        sources=[make_source(ArtifactProcessingState.INDEXED)],
    )
    assert mirror_filter([artifact]) == [artifact]


def test_mirror_filter_includes_indexed_artifact_from_previous_year() -> None:
    last_year = date.today() - timedelta(days=365)
    artifact = make_artifact(
        released=last_year,
        sources=[make_source(ArtifactProcessingState.INDEXED)],
    )
    result = mirror_filter([artifact])
    # Should include if within current year - 1
    if last_year.year >= date.today().year - 1:
        assert artifact in result


def test_mirror_filter_excludes_already_mirrored() -> None:
    artifact = make_artifact(
        released=date.today(),
        sources=[make_source(ArtifactProcessingState.MIRRORED)],
    )
    assert mirror_filter([artifact]) == []


def test_mirror_filter_excludes_old_artifacts() -> None:
    two_years_ago = date.today().replace(year=date.today().year - 2)
    artifact = make_artifact(
        released=two_years_ago,
        sources=[make_source(ArtifactProcessingState.INDEXED)],
    )
    assert mirror_filter([artifact]) == []


def test_mirror_filter_excludes_artifacts_without_release_date() -> None:
    artifact = make_artifact(
        released=None,
        sources=[make_source(ArtifactProcessingState.INDEXED)],
    )
    assert mirror_filter([artifact]) == []


def test_mirror_filter_includes_if_any_source_indexed() -> None:
    """If at least one source is INDEXED, include the artifact."""
    artifact = make_artifact(
        released=date.today(),
        sources=[
            make_source(ArtifactProcessingState.MIRRORED),
            make_source(ArtifactProcessingState.INDEXED),
        ],
    )
    assert mirror_filter([artifact]) == [artifact]


# --- extract_filter tests ---


def test_extract_filter_includes_mirrored_artifact() -> None:
    artifact = make_artifact(sources=[make_source(ArtifactProcessingState.MIRRORED)])
    assert extract_filter([artifact]) == [artifact]


def test_extract_filter_excludes_indexed_artifact() -> None:
    artifact = make_artifact(sources=[make_source(ArtifactProcessingState.INDEXED)])
    assert extract_filter([artifact]) == []


def test_extract_filter_excludes_already_extracted() -> None:
    artifact = make_artifact(sources=[make_source(ArtifactProcessingState.SYMBOLS_EXTRACTED)])
    assert extract_filter([artifact]) == []


def test_extract_filter_includes_if_any_source_mirrored() -> None:
    artifact = make_artifact(
        sources=[
            make_source(ArtifactProcessingState.SYMBOLS_EXTRACTED),
            make_source(ArtifactProcessingState.MIRRORED),
        ],
    )
    assert extract_filter([artifact]) == [artifact]
