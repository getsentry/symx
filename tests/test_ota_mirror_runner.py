"""Tests for OTA mirror workflow state transitions."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from symx.download import DownloadError
from symx.model import ArtifactProcessingState
from symx.ota.mirror import download_ota_from_apple
from symx.ota.model import OtaArtifact, OtaMetaData
from symx.ota.runners import OtaMirror
from tests.fakes import FakeTimeout


def make_ota_artifact() -> OtaArtifact:
    return OtaArtifact(
        id="artifact-id",
        build="23L773",
        version="26.6",
        platform="tvos",
        url="https://updates.cdn-apple.com/ota.zip",
        hash="expected-sha1",
        hash_algorithm="SHA-1",
        description=[],
        devices=["AppleTV6,2"],
        download_path=None,
    )


class InMemoryOtaStorage:
    def __init__(self) -> None:
        self.artifacts: OtaMetaData = {}
        self.saved_otas: list[str] = []

    def save_meta(self, theirs: OtaMetaData) -> OtaMetaData:
        self.artifacts.update(theirs)
        return self.artifacts

    def save_ota(self, ota_meta_key: str, ota_meta: OtaArtifact, ota_file: Path) -> None:
        self.saved_otas.append(ota_meta_key)

    def load_meta(self) -> OtaMetaData | None:
        return self.artifacts

    def load_ota(self, ota: OtaArtifact, download_dir: Path) -> Path | None:
        raise NotImplementedError

    def name(self) -> str:
        return "memory"

    def update_meta_item(self, ota_meta_key: str, ota_meta: OtaArtifact) -> OtaMetaData:
        self.artifacts[ota_meta_key] = ota_meta
        return self.artifacts

    def upload_symbols(self, input_dir: Path, ota_meta_key: str, ota_meta: OtaArtifact, bundle_id: str) -> None:
        raise NotImplementedError


class FakeMetaRetriever:
    def __init__(self, meta: OtaMetaData) -> None:
        self.meta = meta

    def retrieve(self) -> OtaMetaData:
        return self.meta


class FailingDownloader:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def download(self, ota_meta: OtaArtifact, download_dir: Path) -> Path:
        raise self.error


def test_download_failure_marks_ota_mirroring_failed() -> None:
    storage = InMemoryOtaStorage()
    artifact = make_ota_artifact()
    mirror = OtaMirror(
        storage,
        meta_retriever=FakeMetaRetriever({"artifact-key": artifact}),
        downloader=FailingDownloader(DownloadError("transient download failure")),
    )

    mirror.mirror(FakeTimeout(timedelta(minutes=5)))

    assert storage.saved_otas == []
    assert storage.artifacts["artifact-key"].processing_state == ArtifactProcessingState.MIRRORING_FAILED


def test_validation_failure_still_marks_ota_indexed_invalid() -> None:
    storage = InMemoryOtaStorage()
    artifact = make_ota_artifact()
    mirror = OtaMirror(
        storage,
        meta_retriever=FakeMetaRetriever({"artifact-key": artifact}),
        downloader=FailingDownloader(RuntimeError("hash verification failed")),
    )

    mirror.mirror(FakeTimeout(timedelta(minutes=5)))

    assert storage.saved_otas == []
    assert storage.artifacts["artifact-key"].processing_state == ArtifactProcessingState.INDEXED_INVALID


def test_real_ota_downloader_preserves_download_error(tmp_path: Path) -> None:
    error = DownloadError("transient download failure")

    with patch("symx.ota.mirror.try_download_url_to_file", side_effect=error):
        with pytest.raises(DownloadError) as exc_info:
            download_ota_from_apple(make_ota_artifact(), tmp_path)

    assert exc_info.value is error
