"""Tests for the IPSW extract runner using fully mocked side-effects."""

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import HttpUrl

from symx.model import ArtifactProcessingState
from symx.ipsw.model import (
    IpswArtifact,
    IpswPlatform,
    IpswReleaseStatus,
    IpswSource,
)
from symx.ipsw.extract import IpswExtractionRequest
from symx.ipsw.runners import ExtractionResult, extract
from tests.fakes import FakeTimeout
from tests.ipsw_storage_mock import InMemoryIpswStorage


class FakeExtractor:
    """Creates fake symbols, then calls after_operation if provided. Does not call it on failure."""

    def __init__(self, should_fail: bool = False, *, after_operation: Callable[[], None] | None = None) -> None:
        self._should_fail = should_fail
        self._after_operation = after_operation
        self.extractions: list[IpswExtractionRequest] = []
        self.validate_called = False

    def validate_deps(self) -> None:
        self.validate_called = True

    def extract(self, request: IpswExtractionRequest) -> ExtractionResult:
        self.extractions.append(request)

        if self._should_fail:
            raise RuntimeError("extraction failed")

        symbols_dir = request.processing_dir / "symbols"
        symbols_dir.mkdir(parents=True, exist_ok=True)
        # Write a fake symbol binary
        (symbols_dir / "fake.sym").write_bytes(b"symbols")

        bundle_id = f"ipsw_{request.ipsw_path.name[:-5]}"
        result = ExtractionResult(
            symbols_dir=symbols_dir,
            prefix=str(request.platform).lower(),
            bundle_id=bundle_id,
        )
        if self._after_operation is not None:
            self._after_operation()
        return result


# -- Helpers --


def _make_mirrored_artifact(
    storage: InMemoryIpswStorage,
    platform: IpswPlatform = IpswPlatform.IOS,
    version: str = "18.0",
    build: str = "22A100",
    url: str = "https://updates.cdn-apple.com/iOS/iPhone_18.0_22A100_Restore.ipsw",
) -> IpswArtifact:
    """Create a MIRRORED artifact and seed both the db and the mirror file."""
    mirror_path = f"mirror/ipsw/{platform}/{version}/{build}/iPhone_{version}_{build}_Restore.ipsw"
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
                mirror_path=mirror_path,
            )
        ],
    )
    storage.seed_artifact(artifact)

    # Place a fake IPSW file in the mirror location so download_ipsw succeeds
    mirror_file = storage.local_dir / mirror_path
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
        assert extractor.extractions[0] == IpswExtractionRequest(
            platform=IpswPlatform.IOS,
            ipsw_path=storage.local_dir / "iPhone_18.0_22A100_Restore.ipsw",
            processing_dir=storage.local_dir,
            version="18.0",
            build="22A100",
            devices=("iPhone15,2",),
        )

        # Symbols were uploaded
        assert len(storage.uploaded_symbols) == 1

        # Only the outer runner persists the final source state.
        assert storage.meta_updates == [artifact.key]
        updated = storage.get_artifact(artifact.key)
        assert updated is not None
        assert updated.sources[0].processing_state == ArtifactProcessingState.SYMBOLS_EXTRACTED

        # clean_local_dir was called
        assert storage.clean_local_dir_count == 1

    def test_extraction_failure_marks_failed(self, tmp_path: Path) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        artifact = _make_mirrored_artifact(storage)

        after_operation = Mock()
        extractor = FakeExtractor(should_fail=True, after_operation=after_operation)

        extract(storage, FakeTimeout(timedelta(minutes=60)), extractor=extractor)

        after_operation.assert_not_called()

        # No symbols uploaded
        assert len(storage.uploaded_symbols) == 0

        # State is SYMBOL_EXTRACTION_FAILED
        updated = storage.get_artifact(artifact.key)
        assert updated is not None
        assert updated.sources[0].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED

        # Meta was still updated once and local dir cleaned
        assert storage.meta_updates == [artifact.key]
        assert storage.clean_local_dir_count == 1

    def test_upload_failure_marks_failed_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        storage = InMemoryIpswStorage(tmp_path)
        artifact = _make_mirrored_artifact(storage)

        def fail_upload(prefix: str, bundle_id: str, binary_dir: Path) -> None:
            assert storage.meta_updates == []
            assert artifact.sources[0].processing_state == ArtifactProcessingState.MIRRORED
            raise RuntimeError("upload failed")

        monkeypatch.setattr(storage, "upload_symbols", fail_upload)

        extract(storage, FakeTimeout(timedelta(minutes=60)), extractor=FakeExtractor())

        assert storage.meta_updates == [artifact.key]
        assert artifact.sources[0].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
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
                    # No file at mirror_path -> download_ipsw will return None
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
        extractor = FakeExtractor(after_operation=lambda: timer.advance(11))

        extract(storage, timer, extractor=extractor)

        # Only one artifact processed before timeout
        assert len(extractor.extractions) == 1
        assert timer.elapsed_seconds == 11

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
