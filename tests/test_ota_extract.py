"""
Tests for OTA extraction workflow state transitions.

Uses mock storage and injected test doubles to test the orchestration logic
without actual file downloads or subprocess calls.
"""

import signal
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from symx.model import ArtifactProcessingState
from symx.ota.model.ipsw_report import OtaDscReport, OtaDscReportError
from symx.ota.model.materialization import (
    OtaDscMaterializationError,
    OtaDscProcessTerminatedError,
    OtaDscUnavailable,
    OtaDscUnavailableReason,
)
from symx.ota.model import (
    OtaArtifact,
    OtaDelivery,
    OtaExtractError,
    OtaExtractionRequest,
    OtaExtractionResult,
    OtaExtractionSkipped,
    OtaExtractionSkipReason,
    OtaMetaData,
    OtaSymbolsExtracted,
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
    release_type: str | None = None,
    asset_type: str | None = None,
    delivery: OtaDelivery | None = None,
    prerequisite_build: str | None = None,
    prerequisite_version: str | None = None,
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
        release_type=release_type,
        asset_type=asset_type,
        delivery=delivery,
        prerequisite_build=prerequisite_build,
        prerequisite_version=prerequisite_version,
    )


class MockStorage:
    """In-memory storage for testing state transitions."""

    def __init__(self, artifacts: OtaMetaData | None = None):
        self.artifacts = artifacts or {}
        self.load_ota_returns: Path | None = None
        self.uploaded_symbols: list[tuple[str, str]] = []
        self.meta_updates: list[tuple[str, ArtifactProcessingState]] = []

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
        self.meta_updates.append((ota_meta_key, ota_meta.processing_state))
        self.artifacts[ota_meta_key] = ota_meta
        return self.artifacts

    def upload_symbols(self, prefix: str, bundle_id: str, binary_dir: Path) -> None:
        self.uploaded_symbols.append((prefix, bundle_id))


class FakeOtaExtractor:
    """Fake extractor with configured outcomes; calls after_operation on completion, including skips, but not errors."""

    def __init__(
        self,
        *,
        result: OtaExtractionResult | None = None,
        error: Exception | None = None,
        after_operation: Callable[[], None] | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self._after_operation = after_operation
        self.extractions: list[OtaExtractionRequest] = []
        self.validate_called = False

    def validate_deps(self) -> None:
        self.validate_called = True

    def extract(self, request: OtaExtractionRequest) -> OtaExtractionResult:
        self.extractions.append(request)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            result = self._result
        else:
            symbols_dir = request.work_dir / "symbols" / request.bundle_id
            symbols_dir.mkdir(parents=True, exist_ok=True)
            (symbols_dir / "fake.sym").write_bytes(b"symbols")
            result = OtaSymbolsExtracted(symbol_dirs=(symbols_dir,))
        if self._after_operation is not None:
            self._after_operation()
        return result


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
    assert storage.artifacts["key1"].last_modified is not None


def test_extract_marks_failed_extraction(tmp_path: Path) -> None:
    """If extraction fails with OtaExtractError, mark as SYMBOL_EXTRACTION_FAILED."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    after_operation = Mock()
    extractor = FakeOtaExtractor(error=OtaExtractError("test"), after_operation=after_operation)

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    after_operation.assert_not_called()
    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED


def test_terminated_materializer_aborts_worker_without_blame_or_state_change(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    capture = Mock()
    metric = Mock()
    monkeypatch.setattr("symx.ota.runners.sentry_sdk.capture_exception", capture)
    monkeypatch.setattr("symx.ota.runners.sentry_sdk.metrics.count", metric)
    artifact = make_ota_artifact(id="key1", version="27.2")
    next_artifact = make_ota_artifact(id="key2", version="27.1")
    before = [item.model_dump() for item in (artifact, next_artifact)]
    storage = MockStorage({"key1": artifact, "key2": next_artifact})

    def fake_load(ota: OtaArtifact, download_dir: Path) -> Path:
        assert ota is artifact
        ota_file = download_dir / "test.zip"
        ota_file.write_bytes(b"downloaded OTA")
        (download_dir / "partial-output").write_bytes(b"partial")
        return ota_file

    monkeypatch.setattr(storage, "load_ota", fake_load)
    error = OtaDscProcessTerminatedError(signal.SIGKILL, b"RawImagePatch returned -1")
    extractor = FakeOtaExtractor(error=error)

    with pytest.raises(OtaDscProcessTerminatedError, match="SIGKILL") as exc_info:
        OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert exc_info.value is error
    capture.assert_called_once_with(error)
    metric.assert_called_once_with(
        "ota.extract.process_terminated", 1, attributes={"platform": "ios", "signal": "SIGKILL"}
    )
    assert "aborting worker without changing artifact metadata" in caplog.text

    assert [item.model_dump() for item in (artifact, next_artifact)] == before
    assert storage.meta_updates == []
    assert storage.uploaded_symbols == []
    assert len(extractor.extractions) == 1
    assert all(not request.work_dir.exists() for request in extractor.extractions)


def test_ordinary_extraction_failure_still_continues_to_next_artifact(tmp_path: Path) -> None:
    storage = MockStorage(
        {
            "first": make_ota_artifact(id="first", version="27.2"),
            "second": make_ota_artifact(id="second", version="27.1"),
        }
    )
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    class FailFirstExtractor(FakeOtaExtractor):
        def extract(self, request: OtaExtractionRequest) -> OtaExtractionResult:
            if not self.extractions:
                self.extractions.append(request)
                raise OtaExtractError("ordinary artifact failure")
            return super().extract(request)

    extractor = FailFirstExtractor()
    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert len(extractor.extractions) == 2
    assert storage.meta_updates == [
        ("first", ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED),
        ("second", ArtifactProcessingState.SYMBOLS_EXTRACTED),
    ]
    assert len(storage.uploaded_symbols) == 1


def test_payload_extract_materialization_failure_is_marked_symbol_extraction_failed(tmp_path: Path) -> None:
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file
    report = OtaDscReport(
        schema_version=1,
        complete=False,
        files=[],
        errors=[
            OtaDscReportError(
                phase="payload-extract",
                source="payloadv2",
                message="transient I/O failure",
            )
        ],
    )
    unavailable = OtaDscUnavailable(
        reason=OtaDscUnavailableReason.INCOMPLETE,
        report=report,
        message="incomplete materialization",
    )
    extractor = FakeOtaExtractor(error=OtaDscMaterializationError(unavailable))

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
    assert request.owns_local_ota is True
    assert len(storage.uploaded_symbols) == 1
    assert storage.uploaded_symbols[0] == ("ios", "ota_key1")
    assert storage.meta_updates == [("key1", ArtifactProcessingState.SYMBOLS_EXTRACTED)]


@pytest.mark.parametrize("upload_fails", [False, True])
def test_all_symbol_directories_upload_before_success_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upload_fails: bool
) -> None:
    artifact = make_ota_artifact(id="key1")
    storage = MockStorage({"key1": artifact})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file
    symbol_dirs = (tmp_path / "symbols-one", tmp_path / "symbols-two")
    for symbol_dir in symbol_dirs:
        symbol_dir.mkdir()
    extractor = FakeOtaExtractor(result=OtaSymbolsExtracted(symbol_dirs=symbol_dirs))
    uploaded_dirs: list[Path] = []

    def upload(prefix: str, bundle_id: str, binary_dir: Path) -> None:
        assert prefix == artifact.platform
        assert bundle_id == "ota_key1"
        assert storage.meta_updates == []
        assert artifact.processing_state == ArtifactProcessingState.MIRRORED
        uploaded_dirs.append(binary_dir)
        if upload_fails and binary_dir == symbol_dirs[-1]:
            raise RuntimeError("upload failed")

    monkeypatch.setattr(storage, "upload_symbols", upload)
    runner = OtaExtract(storage, extractor=extractor)
    timer = FakeTimeout(timedelta(minutes=5))

    if upload_fails:
        # Preserve OTA's existing fail-fast behavior for non-extraction errors.
        # Earlier uploaded directories must not leave a premature success state.
        with pytest.raises(RuntimeError, match="upload failed"):
            runner.extract(timer)
        assert storage.meta_updates == []
        assert artifact.processing_state == ArtifactProcessingState.MIRRORED
    else:
        runner.extract(timer)
        assert storage.meta_updates == [("key1", ArtifactProcessingState.SYMBOLS_EXTRACTED)]
    assert uploaded_dirs == list(symbol_dirs)


def test_delta_ota_skipped(tmp_path: Path) -> None:
    """Delta OTAs are marked DELTA_OTA and skipped."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = FakeOtaExtractor(
        result=OtaExtractionSkipped(reason=OtaExtractionSkipReason.DELTA),
    )

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.DELTA_OTA


def test_recovery_ota_skipped(tmp_path: Path) -> None:
    """Recovery OTAs are marked RECOVERY_OTA and skipped."""
    storage = MockStorage(
        {
            "key1": make_ota_artifact(
                id="key1",
                release_type="Darwin Recovery",
                asset_type="com.apple.MobileAsset.RecoveryOSUpdate",
                delivery="delta",
                prerequisite_build="20A99",
                prerequisite_version="16.6",
            )
        }
    )
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = FakeOtaExtractor(
        result=OtaExtractionSkipped(reason=OtaExtractionSkipReason.RECOVERY),
    )

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    request = extractor.extractions[0]
    assert request.release_type == "Darwin Recovery"
    assert request.asset_type == "com.apple.MobileAsset.RecoveryOSUpdate"
    assert request.delivery == "delta"
    assert request.prerequisite_build == "20A99"
    assert request.prerequisite_version == "16.6"
    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.RECOVERY_OTA


def test_unsupported_payload_ota_skipped(tmp_path: Path) -> None:
    """OTAs unsupported by current payload tooling are terminal-stated and skipped."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = FakeOtaExtractor(
        result=OtaExtractionSkipped(reason=OtaExtractionSkipReason.UNSUPPORTED_PAYLOAD),
    )

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.UNSUPPORTED_OTA_PAYLOAD


@pytest.mark.parametrize("result", [None, OtaExtractionSkipped(reason=OtaExtractionSkipReason.DELTA)])
def test_timeout_stops_processing(tmp_path: Path, result: OtaExtractionResult | None) -> None:
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
    extractor = FakeOtaExtractor(result=result, after_operation=lambda: timer.advance(11))

    OtaExtract(storage, extractor=extractor).extract(timer)

    # Only one processed before timeout
    assert len(extractor.extractions) == 1
    assert timer.elapsed_seconds == 11


def test_no_artifacts_is_noop() -> None:
    """Empty storage is a no-op."""
    storage = MockStorage()
    extractor = FakeOtaExtractor()

    OtaExtract(storage, extractor=extractor).extract(FakeTimeout(timedelta(minutes=5)))

    assert extractor.validate_called
    assert len(extractor.extractions) == 0
