import base64
import hashlib
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage import Bucket

from symx.gcs import (
    _fs_md5_hash,
    compare_md5_hash,
    parse_gcs_url,
    try_download_to_filename,
    upload_symbol_binaries,
)


def test_parse_gcs_url_valid() -> None:
    uri = parse_gcs_url("gs://my-bucket/prefix")
    assert uri is not None
    assert uri.scheme == "gs"
    assert uri.hostname == "my-bucket"
    assert uri.path == "/prefix"


def test_parse_gcs_url_rejects_non_gs_scheme() -> None:
    assert parse_gcs_url("s3://my-bucket") is None
    assert parse_gcs_url("https://example.com") is None


def test_parse_gcs_url_rejects_missing_bucket() -> None:
    assert parse_gcs_url("gs://") is None


def test_fs_md5_hash_matches_reference(tmp_path: Path) -> None:
    content = b"the quick brown fox jumps over the lazy dog"
    file = tmp_path / "f.txt"
    file.write_bytes(content)

    expected = base64.b64encode(hashlib.md5(content).digest()).decode()
    assert _fs_md5_hash(file) == expected


def test_fs_md5_hash_large_file_streams(tmp_path: Path) -> None:
    content = b"x" * (1024 * 1024 + 7)
    file = tmp_path / "big.bin"
    file.write_bytes(content)

    expected = base64.b64encode(hashlib.md5(content).digest()).decode()
    assert _fs_md5_hash(file) == expected


def test_compare_md5_hash_match(tmp_path: Path) -> None:
    content = b"payload"
    file = tmp_path / "f.bin"
    file.write_bytes(content)
    local_hash = base64.b64encode(hashlib.md5(content).digest()).decode()

    blob = MagicMock()
    blob.md5_hash = local_hash
    blob.name = "remote/f.bin"

    assert compare_md5_hash(file, blob) is True
    blob.reload.assert_called_once()


def test_compare_md5_hash_mismatch(tmp_path: Path) -> None:
    file = tmp_path / "f.bin"
    file.write_bytes(b"payload")

    blob = MagicMock()
    blob.md5_hash = base64.b64encode(hashlib.md5(b"different").digest()).decode()
    blob.name = "remote/f.bin"

    assert compare_md5_hash(file, blob) is False


def test_try_download_to_filename_retries_then_succeeds(tmp_path: Path) -> None:
    local = tmp_path / "blob.bin"
    attempts = 0

    def flaky(path: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("transient")
        Path(path).write_bytes(b"ok")

    blob = MagicMock()
    blob.name = "remote/blob.bin"
    blob.download_to_filename.side_effect = flaky

    assert try_download_to_filename(blob, local, num_retries=5) is True
    assert attempts == 3


def test_try_download_to_filename_all_retries_fail(tmp_path: Path) -> None:
    local = tmp_path / "blob.bin"
    blob = MagicMock()
    blob.name = "remote/blob.bin"
    blob.download_to_filename.side_effect = ConnectionError("nope")

    assert try_download_to_filename(blob, local, num_retries=3) is False
    assert blob.download_to_filename.call_count == 3


class _FakeBlob:
    def __init__(self, name: str, existing: set[str]) -> None:
        self.name = name
        self._existing = existing
        self.uploaded = False

    def exists(self) -> bool:
        return self.name in self._existing

    def upload_from_filename(self, filename: str, retry: object, if_generation_match: int) -> None:
        assert if_generation_match == 0
        if self.name in self._existing:
            raise PreconditionFailed("already exists")
        self._existing.add(self.name)
        self.uploaded = True


class _FakeBucket:
    def __init__(self, existing: set[str] | None = None) -> None:
        self._existing: set[str] = existing or set()
        self.blobs: list[_FakeBlob] = []

    def blob(self, name: str) -> _FakeBlob:
        b = _FakeBlob(name, self._existing)
        self.blobs.append(b)
        return b


def _make_symbol_tree(root: Path) -> None:
    (root / "debugids" / "ab" / "cd").mkdir(parents=True)
    (root / "debugids" / "ab" / "cd" / "symbol1").write_bytes(b"sym1")
    (root / "debugids" / "ef" / "12").mkdir(parents=True)
    (root / "debugids" / "ef" / "12" / "symbol2").write_bytes(b"sym2")
    (root / "meta").mkdir()
    (root / "meta" / "info").write_bytes(b"meta")


def test_upload_symbol_binaries_uploads_all_files_with_prefix(tmp_path: Path) -> None:
    binary_dir = tmp_path / "bundle"
    binary_dir.mkdir()
    _make_symbol_tree(binary_dir)
    bucket = _FakeBucket()

    upload_symbol_binaries(cast(Bucket, bucket), "ios", "ipsw_iphone15_1_21A5248v", binary_dir)

    # bundle-index probe + 3 file blobs
    blob_names = {b.name for b in bucket.blobs}
    assert "symbols/ios/bundles/ipsw_iphone15_1_21A5248v" in blob_names  # bundle-index existence probe
    assert "symbols/debugids/ab/cd/symbol1" in blob_names
    assert "symbols/debugids/ef/12/symbol2" in blob_names
    assert "symbols/meta/info" in blob_names

    uploaded = [b for b in bucket.blobs if b.uploaded]
    assert len(uploaded) == 3


def test_upload_symbol_binaries_counts_duplicates(tmp_path: Path) -> None:
    binary_dir = tmp_path / "bundle"
    binary_dir.mkdir()
    _make_symbol_tree(binary_dir)

    # Pre-seed one of the blobs so it raises PreconditionFailed on upload
    pre_existing = {"symbols/debugids/ab/cd/symbol1"}
    bucket = _FakeBucket(existing=pre_existing)

    upload_symbol_binaries(cast(Bucket, bucket), "macos", "ipsw_mac_14_0", binary_dir)

    uploaded = [b for b in bucket.blobs if b.uploaded]
    # symbol2 + meta/info are new; symbol1 is duplicate
    assert len(uploaded) == 2
    assert all(b.name != "symbols/debugids/ab/cd/symbol1" for b in uploaded)
