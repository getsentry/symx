"""The real storage adapters upload symbols without owning extraction state."""

from pathlib import Path
from unittest.mock import patch

import pytest

from symx.ipsw.storage.gcs import IpswGcsStorage
from symx.ota.storage.gcs import OtaGcsStorage


@pytest.mark.parametrize("upload_fails", [False, True])
def test_ipsw_symbol_upload_does_not_persist_metadata(tmp_path: Path, upload_fails: bool) -> None:
    with (
        patch("symx.ipsw.storage.gcs.Client"),
        patch("symx.ipsw.storage.gcs.upload_symbol_binaries") as upload,
    ):
        storage = IpswGcsStorage(tmp_path, None, "test-bucket")
        with patch.object(storage, "update_meta_item") as update:
            if upload_fails:
                upload.side_effect = RuntimeError("upload failed")
                with pytest.raises(RuntimeError, match="upload failed"):
                    storage.upload_symbols("ios", "ipsw_test", tmp_path)
            else:
                storage.upload_symbols("ios", "ipsw_test", tmp_path)

            upload.assert_called_once_with(storage.bucket, "ios", "ipsw_test", tmp_path)
            update.assert_not_called()


@pytest.mark.parametrize("upload_fails", [False, True])
def test_ota_symbol_upload_does_not_persist_metadata(tmp_path: Path, upload_fails: bool) -> None:
    with (
        patch("symx.ota.storage.gcs.Client"),
        patch("symx.ota.storage.gcs.upload_symbol_binaries") as upload,
    ):
        storage = OtaGcsStorage(None, "test-bucket")
        with patch.object(storage, "update_meta_item") as update:
            if upload_fails:
                upload.side_effect = RuntimeError("upload failed")
                with pytest.raises(RuntimeError, match="upload failed"):
                    storage.upload_symbols("ios", "ota_test", tmp_path)
            else:
                storage.upload_symbols("ios", "ota_test", tmp_path)

            upload.assert_called_once_with(storage.bucket, "ios", "ota_test", tmp_path)
            update.assert_not_called()
