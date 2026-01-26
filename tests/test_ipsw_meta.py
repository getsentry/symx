"""
Tests for IPSW artifact comparison logic.

compare_artifacts_with_diff() decides which changes from AppleDB are significant
(version, build, hashes changed) vs. workflow noise (processing_state, mirror_path).
"""

from datetime import date

from pydantic import HttpUrl

from symx._common import ArtifactProcessingState
from symx._ipsw.common import (
    IpswArtifact,
    IpswArtifactHashes,
    IpswPlatform,
    IpswReleaseStatus,
    IpswSource,
)
from symx._ipsw.meta_sync.appledb import compare_artifacts_with_diff


def make_ipsw_source(
    devices: list[str] | None = None,
    link: str = "https://updates.cdn-apple.com/2024/fullrestores/test/Test_1.0_ABC123_Restore.ipsw",
    hashes: IpswArtifactHashes | None = None,
    size: int | None = None,
    processing_state: ArtifactProcessingState = ArtifactProcessingState.INDEXED,
    mirror_path: str | None = None,
) -> IpswSource:
    return IpswSource(
        devices=devices or ["iPhone14,7"],
        link=HttpUrl(link),
        hashes=hashes,
        size=size,
        processing_state=processing_state,
        mirror_path=mirror_path,
    )


def make_ipsw_artifact(
    platform: IpswPlatform = IpswPlatform.IOS,
    version: str = "17.0",
    build: str = "21A100",
    released: date | None = None,
    release_status: IpswReleaseStatus = IpswReleaseStatus.RELEASE,
    sources: list[IpswSource] | None = None,
) -> IpswArtifact:
    return IpswArtifact(
        platform=platform,
        version=version,
        build=build,
        released=released,
        release_status=release_status,
        sources=sources or [make_ipsw_source()],
    )


# --- Significant changes (should be flagged) ---

def test_identical_artifacts_no_changes() -> None:
    artifact = make_ipsw_artifact()
    has_changes, _ = compare_artifacts_with_diff(artifact, artifact)
    assert not has_changes


def test_different_version_is_significant() -> None:
    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(version="17.0"),
        make_ipsw_artifact(version="17.1"),
    )
    assert has_changes


def test_different_build_is_significant() -> None:
    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(build="21A100"),
        make_ipsw_artifact(build="21A101"),
    )
    assert has_changes


def test_different_released_date_is_significant() -> None:
    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(released=date(2024, 1, 1)),
        make_ipsw_artifact(released=date(2024, 1, 15)),
    )
    assert has_changes


def test_different_source_hashes_is_significant() -> None:
    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(sources=[make_ipsw_source(hashes=IpswArtifactHashes(sha1="abc", sha2=None))]),
        make_ipsw_artifact(sources=[make_ipsw_source(hashes=IpswArtifactHashes(sha1="def", sha2=None))]),
    )
    assert has_changes


def test_added_source_is_significant() -> None:
    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(sources=[make_ipsw_source()]),
        make_ipsw_artifact(sources=[
            make_ipsw_source(),
            make_ipsw_source(devices=["iPhone15,2"], link="https://example.com/other.ipsw"),
        ]),
    )
    assert has_changes


def test_removed_source_is_significant() -> None:
    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(sources=[
            make_ipsw_source(),
            make_ipsw_source(devices=["iPhone15,2"], link="https://example.com/other.ipsw"),
        ]),
        make_ipsw_artifact(sources=[make_ipsw_source()]),
    )
    assert has_changes


# --- Ignored changes (workflow state, not AppleDB data) ---

def test_processing_state_change_is_ignored() -> None:
    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(sources=[make_ipsw_source(processing_state=ArtifactProcessingState.INDEXED)]),
        make_ipsw_artifact(sources=[make_ipsw_source(processing_state=ArtifactProcessingState.MIRRORED)]),
    )
    assert not has_changes


def test_mirror_path_change_is_ignored() -> None:
    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(sources=[make_ipsw_source(mirror_path=None)]),
        make_ipsw_artifact(sources=[make_ipsw_source(mirror_path="mirror/ipsw/test.ipsw")]),
    )
    assert not has_changes


def test_last_run_change_is_ignored() -> None:
    source1 = make_ipsw_source()
    source1.last_run = 100
    source2 = make_ipsw_source()
    source2.last_run = 200

    has_changes, _ = compare_artifacts_with_diff(
        make_ipsw_artifact(sources=[source1]),
        make_ipsw_artifact(sources=[source2]),
    )
    assert not has_changes
