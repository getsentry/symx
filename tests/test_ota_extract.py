"""
Tests for OTA extraction workflow state transitions.

Uses a mock storage to test the orchestration logic without actual
file downloads or subprocess calls.
"""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from symx._common import ArtifactProcessingState
from symx._ota import OtaArtifact, OtaExtract, OtaExtractError, OtaMetaData, OtaStorage


def make_ota_artifact(
    id: str = "abc123",
    processing_state: ArtifactProcessingState = ArtifactProcessingState.MIRRORED,
    download_path: str | None = "mirror/ota/test.zip",
) -> OtaArtifact:
    return OtaArtifact(
        id=id,
        build="21A100",
        version="17.0",
        platform="ios",
        url="https://example.com/ota.zip",
        hash="abc",
        hash_algorithm="SHA-1",
        description=[],
        devices=[],
        download_path=download_path,
        processing_state=processing_state,
    )


class MockStorage(OtaStorage):
    """In-memory storage for testing state transitions."""

    def __init__(self, artifacts: OtaMetaData | None = None):
        self.artifacts = artifacts or {}
        self.load_ota_returns: Path | None = None

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
        pass


def test_extract_resets_missing_ota_to_indexed() -> None:
    """If OTA file is missing from mirror, reset to INDEXED for re-download."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    storage.load_ota_returns = None

    with patch("symx._ota.validate_shell_deps"):
        OtaExtract(storage).extract(timeout=timedelta(minutes=5))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.INDEXED
    assert storage.artifacts["key1"].download_path is None


def test_extract_marks_failed_extraction(tmp_path: Path) -> None:
    """If extraction fails, mark as SYMBOL_EXTRACTION_FAILED."""
    storage = MockStorage({"key1": make_ota_artifact(id="key1")})
    ota_file = tmp_path / "test.zip"
    ota_file.touch()
    storage.load_ota_returns = ota_file

    extractor = OtaExtract(storage)
    with (
        patch("symx._ota.validate_shell_deps"),
        patch.object(extractor, "extract_symbols_from_ota", side_effect=OtaExtractError("test")),
    ):
        extractor.extract(timeout=timedelta(minutes=5))

    assert storage.artifacts["key1"].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED


def test_extract_skips_non_mirrored() -> None:
    """Only MIRRORED artifacts are processed."""
    storage = MockStorage(
        {
            "indexed": make_ota_artifact(id="indexed", processing_state=ArtifactProcessingState.INDEXED),
            "extracted": make_ota_artifact(id="extracted", processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED),
        }
    )

    with patch("symx._ota.validate_shell_deps"):
        OtaExtract(storage).extract(timeout=timedelta(minutes=5))

    assert storage.artifacts["indexed"].processing_state == ArtifactProcessingState.INDEXED
    assert storage.artifacts["extracted"].processing_state == ArtifactProcessingState.SYMBOLS_EXTRACTED
