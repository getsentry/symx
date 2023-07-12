import tempfile
from pathlib import Path

from google.cloud.storage import Blob, Bucket  # type: ignore[import]

from symx._gcs import GoogleStorage
from symx._ipsw.common import ARTIFACTS_META_JSON
from symx._ipsw.meta_sync.appledb import AppleDbIpswImport, IMPORT_STATE_JSON


class IpswGcsStorage:
    def __init__(self, local_dir: Path, bucket: Bucket):
        self.local_dir = local_dir
        self.bucket = bucket

    def load_artifacts_meta(self) -> Blob:
        artifacts_meta_blob = self.bucket.blob(ARTIFACTS_META_JSON)
        if artifacts_meta_blob.exists():
            artifacts_meta_blob.download_to_filename(
                self.local_dir / ARTIFACTS_META_JSON
            )
        return artifacts_meta_blob

    def load_import_state(self) -> Blob:
        import_state_blob = self.bucket.blob(IMPORT_STATE_JSON)
        if import_state_blob.exists():
            import_state_blob.download_to_filename(self.local_dir / IMPORT_STATE_JSON)
        return import_state_blob

    def store_artifacts_meta(self, artifacts_meta_blob: Blob) -> None:
        artifacts_meta_blob.upload_from_filename(
            self.local_dir / ARTIFACTS_META_JSON,
            if_generation_match=artifacts_meta_blob.generation,
        )

    def store_import_state(self, import_state_blob: Blob) -> None:
        import_state_blob.upload_from_filename(
            self.local_dir / IMPORT_STATE_JSON,
            if_generation_match=import_state_blob.generation,
        )


def import_meta_from_appledb(storage: GoogleStorage) -> None:
    with tempfile.TemporaryDirectory() as processing_dir:
        processing_dir_path = Path(processing_dir)

        ipsw_storage = IpswGcsStorage(processing_dir_path, storage.bucket)
        artifacts_meta_blob = ipsw_storage.load_artifacts_meta()
        import_state_blob = ipsw_storage.load_import_state()

        AppleDbIpswImport(processing_dir_path).run()

        ipsw_storage.store_artifacts_meta(artifacts_meta_blob)
        ipsw_storage.store_import_state(import_state_blob)
