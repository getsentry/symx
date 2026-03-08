"""Tests for the IPSW mirror runner using fully mocked side-effects."""

from datetime import date, timedelta
from pathlib import Path

from symx.common import ArtifactProcessingState
from pydantic import HttpUrl

from symx.ipsw.common import (
    IpswArtifact,
    IpswArtifactHashes,
    IpswPlatform,
    IpswReleaseStatus,
    IpswSource,
)
from symx.ipsw.runners import mirror
from tests.fakes import FakeTimeout
from tests.ipsw_storage_mock import InMemoryIpswStorage


# -- Test doubles --


class FakeDownloader:
    """Simulates downloading by writing a dummy file to disk."""

    def __init__(self, verify_result: bool = True) -> None:
        self._verify_result = verify_result
        self.downloads: list[tuple[str, Path]] = []

    def download(self, url: str, filepath: Path) -> None:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_bytes(b"fake ipsw content")
        self.downloads.append((url, filepath))

    def verify(self, filepath: Path, source: IpswSource) -> bool:
        return self._verify_result


# -- Helpers --


def _make_artifact(
    platform: IpswPlatform = IpswPlatform.IOS,
    version: str = "18.0",
    build: str = "22A100",
    released: date | None = None,
    state: ArtifactProcessingState = ArtifactProcessingState.INDEXED,
    url: str = "https://updates.cdn-apple.com/iOS/iPhone_18.0_22A100_Restore.ipsw",
) -> IpswArtifact:
    if released is None:
        released = date.today()
    return IpswArtifact(
        platform=platform,
        version=version,
        build=build,
        released=released,
        release_status=IpswReleaseStatus.RELEASE,
        sources=[
            IpswSource(
                devices=["iPhone15,2"],
                link=HttpUrl(url),
                hashes=IpswArtifactHashes(sha1="abc123", sha2=None),
                size=1024,
                processing_state=state,
            )
        ],
    )


# -- Tests --


class TestMirrorRunner:
    def test_successful_mirror(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        artifact = _make_artifact()
        storage.seed_artifact(artifact)

        downloader = FakeDownloader(verify_result=True)

        mirror(storage, FakeTimeout(timedelta(minutes=60)), downloader=downloader)

        # Download was called
        assert len(downloader.downloads) == 1

        # Storage recorded the upload
        assert len(storage.uploaded_ipsws) == 1
        assert storage.uploaded_ipsws[0] == (artifact.key, "iPhone_18.0_22A100_Restore.ipsw")

        # Artifact state updated to MIRRORED
        updated = storage.get_artifact(artifact.key)
        assert updated is not None
        assert updated.sources[0].processing_state == ArtifactProcessingState.MIRRORED
        assert updated.sources[0].mirror_path is not None

    def test_verification_failure_marks_mirroring_failed(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        artifact = _make_artifact()
        storage.seed_artifact(artifact)

        downloader = FakeDownloader(verify_result=False)

        mirror(storage, FakeTimeout(timedelta(minutes=60)), downloader=downloader)

        # No upload happened
        assert len(storage.uploaded_ipsws) == 0

        # State is MIRRORING_FAILED
        updated = storage.get_artifact(artifact.key)
        assert updated is not None
        assert updated.sources[0].processing_state == ArtifactProcessingState.MIRRORING_FAILED

    def test_already_mirrored_source_is_skipped(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        artifact = _make_artifact(state=ArtifactProcessingState.MIRRORED)
        storage.seed_artifact(artifact)

        downloader = FakeDownloader()

        mirror(storage, FakeTimeout(timedelta(minutes=60)), downloader=downloader)

        # Nothing downloaded or uploaded — already processed
        assert len(downloader.downloads) == 0
        assert len(storage.uploaded_ipsws) == 0

    def test_timeout_stops_processing(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)

        # Seed two artifacts
        a1 = _make_artifact(version="18.0", build="22A100")
        a2 = _make_artifact(
            version="18.1", build="22B100", url="https://updates.cdn-apple.com/iOS/iPhone_18.1_22B100_Restore.ipsw"
        )
        storage.seed_artifact(a1)
        storage.seed_artifact(a2)

        timer = FakeTimeout(timedelta(seconds=10))
        downloader = FakeDownloader()

        # Timeout is 10 seconds, but timer jumps past it after first download
        original_download = downloader.download

        def download_then_advance(url: str, filepath: Path) -> None:
            original_download(url, filepath)
            timer.advance(11)

        downloader.download = download_then_advance  # type: ignore[assignment]

        mirror(storage, timer, downloader=downloader)

        # Only one artifact should have been processed
        assert len(storage.uploaded_ipsws) == 1

    def test_old_artifact_filtered_out(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        # Released 3 years ago — mirror_filter requires this/previous year
        old_artifact = _make_artifact(released=date(2020, 1, 1))
        storage.seed_artifact(old_artifact)

        downloader = FakeDownloader()

        mirror(storage, FakeTimeout(timedelta(minutes=60)), downloader=downloader)

        assert len(downloader.downloads) == 0
        assert len(storage.uploaded_ipsws) == 0

    def test_multiple_sources_on_one_artifact(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        artifact = IpswArtifact(
            platform=IpswPlatform.IOS,
            version="18.0",
            build="22A100",
            released=date.today(),
            release_status=IpswReleaseStatus.RELEASE,
            sources=[
                IpswSource(
                    devices=["iPhone15,2"],
                    link=HttpUrl("https://updates.cdn-apple.com/iOS/iPhone15_2_18.0_22A100_Restore.ipsw"),
                    processing_state=ArtifactProcessingState.INDEXED,
                ),
                IpswSource(
                    devices=["iPhone15,3"],
                    link=HttpUrl("https://updates.cdn-apple.com/iOS/iPhone15_3_18.0_22A100_Restore.ipsw"),
                    processing_state=ArtifactProcessingState.INDEXED,
                ),
            ],
        )
        storage.seed_artifact(artifact)

        downloader = FakeDownloader()

        mirror(storage, FakeTimeout(timedelta(minutes=60)), downloader=downloader)

        # Both sources downloaded and uploaded
        assert len(downloader.downloads) == 2
        assert len(storage.uploaded_ipsws) == 2

    def test_mixed_source_states(self, tmp_path: Path) -> None:
        """Only INDEXED sources are mirrored; others are skipped."""
        storage = InMemoryIpswStorage(tmp_path)
        artifact = IpswArtifact(
            platform=IpswPlatform.IOS,
            version="18.0",
            build="22A100",
            released=date.today(),
            release_status=IpswReleaseStatus.RELEASE,
            sources=[
                IpswSource(
                    devices=["iPhone15,2"],
                    link=HttpUrl("https://updates.cdn-apple.com/iOS/iPhone15_2_18.0_22A100_Restore.ipsw"),
                    processing_state=ArtifactProcessingState.MIRRORED,
                    mirror_path="mirror/ipsw/iOS/18.0/22A100/iPhone15_2_18.0_22A100_Restore.ipsw",
                ),
                IpswSource(
                    devices=["iPhone15,3"],
                    link=HttpUrl("https://updates.cdn-apple.com/iOS/iPhone15_3_18.0_22A100_Restore.ipsw"),
                    processing_state=ArtifactProcessingState.INDEXED,
                ),
            ],
        )
        storage.seed_artifact(artifact)

        downloader = FakeDownloader()

        mirror(storage, FakeTimeout(timedelta(minutes=60)), downloader=downloader)

        # Only the INDEXED source was processed
        assert len(downloader.downloads) == 1
        assert len(storage.uploaded_ipsws) == 1
        assert storage.uploaded_ipsws[0][1] == "iPhone15_3_18.0_22A100_Restore.ipsw"

    def test_no_artifacts_is_noop(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        downloader = FakeDownloader()

        mirror(storage, FakeTimeout(timedelta(minutes=60)), downloader=downloader)

        assert len(downloader.downloads) == 0
        assert len(storage.uploaded_ipsws) == 0
