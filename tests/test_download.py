from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from symx.download import DOWNLOAD_TIMEOUT, DownloadError, download_url_to_file, try_download_url_to_file


def _fake_response(
    chunks: list[bytes],
    content_length: str | None = None,
    status_error: Exception | None = None,
) -> MagicMock:
    """Build a MagicMock that behaves like a `requests.Response` used as a context manager."""
    res = MagicMock()
    res.__enter__.return_value = res
    res.__exit__.return_value = False
    res.headers = {}
    if content_length is not None:
        res.headers["content-length"] = content_length
    res.iter_content = MagicMock(return_value=iter(chunks))
    if status_error is not None:
        res.raise_for_status.side_effect = status_error
    return res


def test_download_url_to_file_writes_chunks(tmp_path: Path) -> None:
    filepath = tmp_path / "out.bin"
    chunks = [b"hello ", b"world"]

    res = _fake_response(chunks, content_length="11")
    with patch("symx.download.requests.get", return_value=res) as mock_get:
        download_url_to_file("https://example.com/file", filepath)

    mock_get.assert_called_once()
    # Verify timeout was passed so stalled CDN connections will fail fast instead of hanging
    _args, kwargs = mock_get.call_args
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == DOWNLOAD_TIMEOUT
    res.raise_for_status.assert_called_once()
    assert filepath.read_bytes() == b"hello world"


def test_download_url_to_file_without_content_length(tmp_path: Path) -> None:
    filepath = tmp_path / "out.bin"
    with patch("symx.download.requests.get", return_value=_fake_response([b"data"])):
        download_url_to_file("https://example.com/file", filepath)
    assert filepath.read_bytes() == b"data"


def test_download_url_to_file_raises_on_http_error(tmp_path: Path) -> None:
    """A 404/500 response must raise, otherwise the error body is silently written to disk."""
    filepath = tmp_path / "out.bin"
    http_error = requests.HTTPError("404 Not Found")
    res = _fake_response([b"<html>not found</html>"], status_error=http_error)

    with patch("symx.download.requests.get", return_value=res):
        with pytest.raises(requests.HTTPError):
            download_url_to_file("https://example.com/missing", filepath)

    # raise_for_status fires before the file is opened for writing, so no partial file is left behind
    assert not filepath.exists()


def test_download_url_to_file_propagates_timeout(tmp_path: Path) -> None:
    """A connect/read timeout must propagate so `try_download_url_to_file` can retry."""
    filepath = tmp_path / "out.bin"
    with patch("symx.download.requests.get", side_effect=requests.Timeout("read timed out")):
        with pytest.raises(requests.Timeout):
            download_url_to_file("https://example.com/slow", filepath)
    assert not filepath.exists()


def test_try_download_retries_then_succeeds(tmp_path: Path) -> None:
    filepath = tmp_path / "out.bin"
    call_count = 0

    def flaky(url: str, filepath: Path) -> None:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("transient")
        filepath.write_bytes(b"ok")

    with patch("symx.download.download_url_to_file", side_effect=flaky):
        try_download_url_to_file("https://example.com/file", filepath, num_retries=5)

    assert call_count == 3
    assert filepath.read_bytes() == b"ok"


def test_try_download_retries_on_http_error(tmp_path: Path) -> None:
    """HTTP errors from the inner function must trigger the retry loop."""
    filepath = tmp_path / "out.bin"
    attempts = 0

    def flaky(url: str, filepath: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise requests.HTTPError("500")
        filepath.write_bytes(b"ok")

    with patch("symx.download.download_url_to_file", side_effect=flaky):
        try_download_url_to_file("https://example.com/flaky", filepath, num_retries=5)

    assert attempts == 2
    assert filepath.read_bytes() == b"ok"


def test_try_download_raises_after_num_retries(tmp_path: Path) -> None:
    """All attempts failing must raise DownloadError chained to the last underlying exception.

    Callers (ipsw/runners.py, ota/mirror.py) rely on this so they can distinguish transport failure
    from hash-verification failure and avoid calling verify on a non-existent file.
    """
    filepath = tmp_path / "out.bin"
    underlying = ConnectionError("nope")

    with patch("symx.download.download_url_to_file", side_effect=underlying) as mock_dl:
        with pytest.raises(DownloadError, match="Failed to download") as exc_info:
            try_download_url_to_file("https://example.com/file", filepath, num_retries=3)

    assert mock_dl.call_count == 3
    assert exc_info.value.__cause__ is underlying
    assert not filepath.exists()
