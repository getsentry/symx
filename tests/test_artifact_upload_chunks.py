from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.cloud.storage import Blob

from symx.gcs import ARTIFACT_UPLOAD_CHUNK_SIZE
from symx.ipsw.storage.gcs import IpswGcsStorage
from symx.ota.storage.gcs import OtaGcsStorage


@pytest.mark.parametrize("kind", ["ota", "ipsw"])
def test_whole_artifact_upload_chunk_size(tmp_path: Path, kind: str) -> None:
    artifact_file = tmp_path / "artifact.zip"
    artifact_file.write_bytes(b"artifact")
    bucket = MagicMock()
    # Use a real Blob to exercise the SDK's chunk-size validation, without IO.
    blob = Blob("mirror/artifact.zip", bucket)
    bucket.blob.return_value = blob
    artifact = MagicMock()

    with (
        patch.object(blob, "exists", return_value=False),
        patch.object(blob, "upload_from_filename") as upload,
    ):
        if kind == "ota":
            with patch("symx.ota.storage.gcs.Client") as client:
                client.return_value.bucket.return_value = bucket
                storage = OtaGcsStorage(None, "test")
            with patch.object(storage, "update_meta_item"):
                storage.save_ota("key", artifact, artifact_file)
        else:
            with patch("symx.ipsw.storage.gcs.Client") as client:
                client.return_value.bucket.return_value = bucket
                storage = IpswGcsStorage(tmp_path, None, "test")
            source = MagicMock()
            artifact.sources = [source]
            storage.upload_ipsw(artifact, (artifact_file, source))

        upload.assert_called_once()
        assert blob.chunk_size == ARTIFACT_UPLOAD_CHUNK_SIZE == 512 * 1024 * 1024
