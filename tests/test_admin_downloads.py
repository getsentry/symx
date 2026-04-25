import hashlib
from pathlib import Path
from unittest.mock import patch

from symx.admin.db import IpswFailureRow, OtaFailureRow
from symx.admin.downloads import download_ipsw_to_cache, download_ota_to_cache, infer_file_name_from_url
from symx.model import ArtifactProcessingState


def test_infer_file_name_from_url_prefers_query_path() -> None:
    url = (
        "https://developer.apple.com/services-account/download?"
        "path=/WWDC_2020/iOS_14_beta/iPhone_4.7_14.0_18A5301v_Restore.ipsw"
    )

    assert infer_file_name_from_url(url) == "iPhone_4.7_14.0_18A5301v_Restore.ipsw"


def test_download_ipsw_to_cache_verifies_sha1(tmp_path: Path) -> None:
    payload = b"ipsw-bytes"
    row = IpswFailureRow(
        last_modified="2024-09-03T12:34:56",
        processing_state=ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
        platform="iOS",
        version="18.0",
        build="22A100",
        artifact_key="iOS_18.0_22A100",
        file_name="test.ipsw",
        link="https://updates.cdn-apple.com/test.ipsw",
        sha1=hashlib.sha1(payload).hexdigest(),
        last_run=123,
        mirror_path=None,
    )

    messages: list[str] = []

    with patch("symx.admin.downloads.try_download_url_to_file") as download_file:
        download_file.side_effect = lambda url, path, status_callback=None: path.write_bytes(payload)
        result = download_ipsw_to_cache(row, tmp_path, status_callback=messages.append)

    assert result.verified is True
    assert result.path.exists()
    assert result.path.read_bytes() == payload
    assert any("Verifying IPSW SHA-1" in message for message in messages)


def test_download_ipsw_to_cache_allows_unverified_download(tmp_path: Path) -> None:
    payload = b"ipsw-bytes"
    row = IpswFailureRow(
        last_modified="2024-09-03T12:34:56",
        processing_state=ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
        platform="iOS",
        version="18.0",
        build="22A100",
        artifact_key="iOS_18.0_22A100",
        file_name="download",
        link="https://developer.apple.com/services-account/download?path=/foo/bar/Test_Restore.ipsw",
        sha1=None,
        last_run=123,
        mirror_path=None,
    )

    with patch("symx.admin.downloads.try_download_url_to_file") as download_file:
        download_file.side_effect = lambda url, path, status_callback=None: path.write_bytes(payload)
        result = download_ipsw_to_cache(row, tmp_path)

    assert result.verified is False
    assert "without SHA verification" in result.message
    assert result.path.name.endswith("Test_Restore.ipsw")


def test_download_ota_to_cache_verifies_sha1(tmp_path: Path) -> None:
    payload = b"ota-bytes"
    row = OtaFailureRow(
        last_run=456,
        processing_state=ArtifactProcessingState.INDEXED_INVALID,
        platform="ios",
        version="18.0",
        build="22A100",
        ota_key="ota-key",
        artifact_id="ota-id",
        url="https://updates.cdn-apple.com/test.zip",
        hash=hashlib.sha1(payload).hexdigest(),
        hash_algorithm="SHA-1",
        download_path=None,
    )

    with patch("symx.admin.downloads.try_download_url_to_file") as download_file:
        download_file.side_effect = lambda url, path, status_callback=None: path.write_bytes(payload)
        result = download_ota_to_cache(row, tmp_path)

    assert result.verified is True
    assert result.path.exists()
    assert result.path.read_bytes() == payload
