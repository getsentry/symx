"""Tests for the IPSW extract runner using fully mocked side-effects."""

from datetime import date, timedelta
from pathlib import Path

from pydantic import HttpUrl

from symx.common import ArtifactProcessingState
from symx.ipsw.common import (
    IpswArtifact,
    IpswPlatform,
    IpswReleaseStatus,
    IpswSource,
)
from symx.ipsw.runners import ExtractionResult, extract
from tests.fakes import FakeTimeout
from tests.ipsw_storage_mock import InMemoryIpswStorage


class FakeExtractor:
    """Simulates extraction by creating a fake symbols directory."""

    def __init__(self, should_fail: bool = False) -> None:
        self._should_fail = should_fail
        self.extractions: list[tuple[IpswPlatform, str]] = []
        self.validate_called = False

    def validate_deps(self) -> None:
        self.validate_called = True

    def extract(
        self, platform: IpswPlatform, file_name: str, processing_dir: Path, ipsw_path: Path
    ) -> ExtractionResult:
        self.extractions.append((platform, file_name))

        if self._should_fail:
            raise RuntimeError("extraction failed")

        symbols_dir = processing_dir / "symbols"
        symbols_dir.mkdir(parents=True, exist_ok=True)
        # Write a fake symbol binary
        (symbols_dir / "fake.sym").write_bytes(b"symbols")

        bundle_id = f"ipsw_{file_name[:-5]}"
        return ExtractionResult(
            symbols_dir=symbols_dir,
            prefix=str(platform).lower(),
            bundle_id=bundle_id,
        )


# -- Helpers --


def _make_mirrored_artifact(
    storage: InMemoryIpswStorage,
    platform: IpswPlatform = IpswPlatform.IOS,
    version: str = "18.0",
    build: str = "22A100",
    url: str = "https://updates.cdn-apple.com/iOS/iPhone_18.0_22A100_Restore.ipsw",
) -> IpswArtifact:
    """Create a MIRRORED artifact and seed both the db and the mirror file."""
    artifact = IpswArtifact(
        platform=platform,
        version=version,
        build=build,
        released=date.today(),
        release_status=IpswReleaseStatus.RELEASE,
        sources=[
            IpswSource(
                devices=["iPhone15,2"],
                link=HttpUrl(url),
                processing_state=ArtifactProcessingState.MIRRORED,
                mirror_path=f"mirror/ipsw/{platform}/{version}/{build}/iPhone_{version}_{build}_Restore.ipsw",
            )
        ],
    )
    storage.seed_artifact(artifact)

    # Place a fake IPSW file in the mirror location so download_ipsw succeeds
    mirror_file = storage.local_dir / artifact.sources[0].mirror_path  # type: ignore[operator]
    mirror_file.parent.mkdir(parents=True, exist_ok=True)
    mirror_file.write_bytes(b"fake ipsw")

    return artifact


# -- Tests --


class TestExtractRunner:
    def test_successful_extraction(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        artifact = _make_mirrored_artifact(storage)

        extractor = FakeExtractor()

        extract(storage, FakeTimeout(timedelta(minutes=60)), extractor=extractor)

        assert extractor.validate_called
        assert len(extractor.extractions) == 1
        assert extractor.extractions[0] == (IpswPlatform.IOS, "iPhone_18.0_22A100_Restore.ipsw")

        # Symbols were uploaded
        assert len(storage.uploaded_symbols) == 1

        # State updated to SYMBOLS_EXTRACTED
        updated = storage.get_artifact(artifact.key)
        assert updated is not None
        assert updated.sources[0].processing_state == ArtifactProcessingState.SYMBOLS_EXTRACTED

        # clean_local_dir was called
        assert storage.clean_local_dir_count == 1

    def test_extraction_failure_marks_failed(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        artifact = _make_mirrored_artifact(storage)

        extractor = FakeExtractor(should_fail=True)

        extract(storage, FakeTimeout(timedelta(minutes=60)), extractor=extractor)

        # No symbols uploaded
        assert len(storage.uploaded_symbols) == 0

        # State is SYMBOL_EXTRACTION_FAILED
        updated = storage.get_artifact(artifact.key)
        assert updated is not None
        assert updated.sources[0].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED

        # Meta was still updated and local dir cleaned
        assert storage.clean_local_dir_count == 1

    def test_mirror_corrupt_when_download_fails(self, tmp_path: Path) -> None:
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
                    link=HttpUrl("https://updates.cdn-apple.com/iOS/iPhone_18.0_22A100_Restore.ipsw"),
                    processing_state=ArtifactProcessingState.MIRRORED,
                    mirror_path="mirror/ipsw/iOS/18.0/22A100/iPhone_18.0_22A100_Restore.ipsw",
                    # No file at mirror_path — download_ipsw will return None
                )
            ],
        )
        storage.seed_artifact(artifact)

        extractor = FakeExtractor()

        extract(storage, FakeTimeout(timedelta(minutes=60)), extractor=extractor)

        # Extractor never called
        assert len(extractor.extractions) == 0

        # State is MIRROR_CORRUPT
        updated = storage.get_artifact(artifact.key)
        assert updated is not None
        assert updated.sources[0].processing_state == ArtifactProcessingState.MIRROR_CORRUPT

        assert storage.clean_local_dir_count == 1

    def test_non_mirrored_source_skipped(self, tmp_path: Path) -> None:
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
                    link=HttpUrl("https://updates.cdn-apple.com/iOS/iPhone_18.0_22A100_Restore.ipsw"),
                    processing_state=ArtifactProcessingState.INDEXED,
                )
            ],
        )
        storage.seed_artifact(artifact)

        extractor = FakeExtractor()

        extract(storage, FakeTimeout(timedelta(minutes=60)), extractor=extractor)

        # extract_filter requires MIRRORED, so nothing happens
        assert len(extractor.extractions) == 0

    def test_timeout_stops_processing(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        _make_mirrored_artifact(storage, version="18.0", build="22A100")
        _make_mirrored_artifact(
            storage,
            version="18.1",
            build="22B100",
            url="https://updates.cdn-apple.com/iOS/iPhone_18.1_22B100_Restore.ipsw",
        )

        timer = FakeTimeout(timedelta(seconds=10))
        extractor = FakeExtractor()

        original_extract = extractor.extract

        def extract_then_advance(
            platform: IpswPlatform, file_name: str, processing_dir: Path, ipsw_path: Path
        ) -> ExtractionResult:
            result = original_extract(platform, file_name, processing_dir, ipsw_path)
            timer.advance(11)
            return result

        extractor.extract = extract_then_advance  # type: ignore[assignment]

        extract(storage, timer, extractor=extractor)

        # Only one artifact processed before timeout
        assert len(extractor.extractions) == 1

    def test_symbols_dir_cleaned_after_upload(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        _make_mirrored_artifact(storage)

        extractor = FakeExtractor()

        extract(storage, FakeTimeout(timedelta(minutes=60)), extractor=extractor)

        # The symbols dir created by FakeExtractor should have been rmtree'd
        symbols_dir = storage.local_dir / "symbols"
        assert not symbols_dir.exists()

    def test_no_artifacts_is_noop(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        extractor = FakeExtractor()

        extract(storage, FakeTimeout(timedelta(minutes=60)), extractor=extractor)

        assert extractor.validate_called
        assert len(extractor.extractions) == 0
        assert len(storage.uploaded_symbols) == 0
