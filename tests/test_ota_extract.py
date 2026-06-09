"""
Tests for OTA extraction workflow state transitions.

Uses mock storage and injected test doubles to test the orchestration logic
without actual file downloads or subprocess calls.
"""

from datetime import timedelta
from pathlib import Path

from symx.model import ArtifactProcessingState
from symx.ota.model import (
    DeltaOtaError,
    OtaArtifact,
    OtaExtractError,
    OtaExtractionRequest,
    OtaMetaData,
    RecoveryOtaError,
    UnsupportedOtaPayloadError,
    parse_version_tuple,
)
from symx.ota.runners import OtaExtract
from symx.ota.storage.gcs import ota_mirror_path
from tests.fakes import FakeTimeout


def make_ota_artifact(
    id: str = "abc123",
    processing_state: ArtifactProcessingState = ArtifactProcessingState.MIRRORED,
    download_path: str | None = "mirror/ota/test.zip",
    platform: str = "ios",
    version: str = "17.0",
    build: str = "21A100",
) -> OtaArtifact:
    return OtaArtifact(
        id=id,
        build=build,
        version=version,
        platform=platform,
        url="https://example.com/ota.zip",
        hash="abc",
        hash_algorithm="SHA-1",
        description=[],
        devices=[],
        download_path=download_path,
        processing_state=processing_state,
    )


class MockStorage:
    """In-memory storage for testing state transitions."""

    def __init__(self, artifacts: OtaMetaData | None = None):
        self.artifacts = artifacts or {}
        self.load_ota_returns: Path | None = None
        self.uploaded_symbols: list[tuple[str, str]] = []

    def save_meta(self, theirs: OtaMetaData) -> OtaMetaData:
        self.artifacts.update(theirs)
        return self.artifacts

    def save_ota(self, ota_meta_key: str, ota_meta: OtaArtifact, ota_file: Path) -> None:
        self.artifacts[ota_meta_key] = ota_meta

    def load_meta(self) -> OtaMetaData | None:
        return self.artifacts

    def load_ota(self, ota: OtaArtifact, download_dir: Path) -> Path | None:
        return self.load_ota_returns

    def name(self) -> str:
        return "mock"

    def update_meta_item(self, ota_meta_key: str, ota_meta: OtaArtifact) -> OtaMetaData:
        self.artifacts[ota_meta_key] = ota_meta
        return self.artifacts

    def upload_symbols(self, input_dir: Path, ota_meta_key: str, ota_meta: OtaArtifact, bundle_id: str) -> None:
        self.uploaded_symbols.append((ota_meta_key, bundle_id))


class FakeOtaExtractor:
    """Fake extractor that creates dummy symbol dirs or raises configured errors."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.extractions: list[OtaExtractionRequest] = []
        self.validate_called = False

    def validate_deps(self) -> None:
        self.validate_called = True

    def extract(self, request: OtaExtractionRequest) -> list[Path]:
        self.extractions.append(request)
        if self._error is not None:
            raise self._error
        symbols_dir = request.work_dir / "symbols" / request.bundle_id
        symbols_dir.mkdir(parents=True, exist_ok=True)
        (symbols_dir / "fake.sym").write_bytes(b"symbols")
        return [symbols_dir]


# -- Tests --


def test_parse_version_tuple_raises_for_unparseable_version() -> None:
    try:
        parse_version_tuple("17.0_beta")
    except ValueError as error:
        assert str(error) == "Unexpected OTA version format: '17.0_beta'"
    else:
        raise AssertionError("parse_version_tuple should reject unparseable versions")


def test_ota_mirror_path_uses_artifact_metadata_instead_of_parsing_file_name() -> None:
    ota = make_ota_artifact(platform="ios", version="17.0", build="21A100")
    ota_file = Path("wrong_99.0_BAD_payload.zip")

    assert ota_mirror_path(ota, ota_file) == "mirror/ota/ios/17.0/21A100/wrong_99.0_BAD_payload.zip"


def test_extract_resets_missing_ota_to_indexed() -> None:
    """If OTA file is missing from mirror, reset to INDEXED for re-download."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    storage.load_ota_returns = None

    OtaExtract(storage, extractor=FakeOtaExtractor()).extract(FakeTimeout(timedelta(minutes=5)))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.INDEXED
    assert storage.artifacts["key1"].download_path is None


def test_extract_marks_failed_extraction(tmp_path: Path) -> None:
    """If extraction fails with OtaExtractError, mark as SYMBOL_EXTRACTION_FAILED."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = FakeOtaExtractor(error=OtaExtractError("test"))

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED


def test_extract_skips_non_mirrored() -> None:
    """Only MIRRORED artifacts are processed."""
    storage = MockStorage(
        {
            "indexed": make_ota_artifact(id="indexed", processing_state=ArtifactProcessingState.INDEXED),
            "extracted": make_ota_artifact(id="extracted", processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED),
        }
    )

    extractor = FakeOtaExtractor()
    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert len(extractor.extractions) == 0
    assert storage.artifacts["indexed"].processing_state == ArtifactProcessingState.INDEXED
    assert storage.artifacts["extracted"].processing_state == ArtifactProcessingState.SYMBOLS_EXTRACTED


def test_successful_extraction(tmp_path: Path) -> None:
    """Happy path: extract symbols and upload them."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = FakeOtaExtractor()

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert len(extractor.extractions) == 1
    request = extractor.extractions[0]
    assert request.local_ota == ota_file
    assert request.platform == "ios"
    assert request.version == "17.0"
    assert request.build == "21A100"
    assert request.bundle_id == "ota_key1"
    assert len(storage.uploaded_symbols) == 1
    assert storage.uploaded_symbols[0] == ("key1", "ota_key1")


def test_delta_ota_skipped(tmp_path: Path) -> None:
    """Delta OTAs are marked DELTA_OTA and skipped."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = FakeOtaExtractor(error=DeltaOtaError("delta"))

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.DELTA_OTA


def test_recovery_ota_skipped(tmp_path: Path) -> None:
    """Recovery OTAs are marked RECOVERY_OTA and skipped."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = FakeOtaExtractor(error=RecoveryOtaError("recovery"))

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.RECOVERY_OTA


def test_unsupported_payload_ota_skipped(tmp_path: Path) -> None:
    """OTAs unsupported by current payload tooling are terminal-stated and skipped."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = FakeOtaExtractor(error=UnsupportedOtaPayloadError("unsupported payload"))

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.UNSUPPORTED_OTA_PAYLOAD


def test_timeout_stops_processing(tmp_path: Path) -> None:
    """Extraction stops when timeout is exceeded."""
    storage = MockStorage(
        {
            "key1": make_ota_artifact(id="key1", version="18.0"),
            "key2": make_ota_artifact(id="key2", version="17.0"),
        }
    )
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    timer = FakeTimeout(timedelta(seconds=10))
    extractor = FakeOtaExtractor()

    original_extract = extractor.extract

    def extract_then_advance(request: OtaExtractionRequest) -> list[Path]:
        result = original_extract(request)
        timer.advance(11)
        return result

    extractor.extract = extract_then_advance  # type: ignore[assignment]

    OtaExtract(storage, extractor=extractor).extract(timer)

    # Only one processed before timeout
    assert len(extractor.extractions) == 1


def test_no_artifacts_is_noop() -> None:
    """Empty storage is a no-op."""
    storage = MockStorage()
    extractor = FakeOtaExtractor()

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert extractor.validate_called
    assert len(extractor.extractions) == 0
