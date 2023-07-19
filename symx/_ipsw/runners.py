import datetime
import logging
import tempfile
from pathlib import Path

from google.cloud.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket  # type: ignore[import]

from symx._gcs import GoogleStorage
from symx._ipsw.common import ARTIFACTS_META_JSON, IpswArtifactDb, IpswArtifact
from symx._ipsw.meta_sync.appledb import AppleDbIpswImport, IMPORT_STATE_JSON

logger = logging.getLogger(__name__)


class IpswGcsStorage:
    def __init__(self, local_dir: Path, bucket: Bucket):
        self.local_dir = local_dir
        self.local_artifacts_meta = self.local_dir / ARTIFACTS_META_JSON
        self.local_import_state = self.local_dir / IMPORT_STATE_JSON
        self.bucket = bucket

    def load_artifacts_meta(self) -> Blob:
        artifacts_meta_blob = self.bucket.blob(ARTIFACTS_META_JSON)
        if artifacts_meta_blob.exists():
            artifacts_meta_blob.download_to_filename(self.local_artifacts_meta)
        return artifacts_meta_blob

    def load_import_state(self) -> Blob:
        import_state_blob = self.bucket.blob(IMPORT_STATE_JSON)
        if import_state_blob.exists():
            import_state_blob.download_to_filename(self.local_import_state)
        return import_state_blob

    def store_artifacts_meta(self, artifacts_meta_blob: Blob) -> None:
        artifacts_meta_blob.upload_from_filename(
            self.local_artifacts_meta,
            if_generation_match=artifacts_meta_blob.generation,
        )

    def store_import_state(self, import_state_blob: Blob) -> None:
        import_state_blob.upload_from_filename(
            self.local_import_state,
            if_generation_match=import_state_blob.generation,
        )

    def update_meta_item(
        self, meta_key: str, ipsw_meta: IpswArtifact
    ) -> IpswArtifactDb:
        retry = 5
        while retry > 0:
            blob = self.load_artifacts_meta()
            if blob.exists():
                try:
                    fp = open(self.local_artifacts_meta)
                except IOError:
                    ours, generation_match_precondition = IpswArtifactDb(), 0
                else:
                    with fp:
                        ours, generation_match_precondition = (
                            IpswArtifactDb.model_validate_json(fp.read()),
                            blob.generation,
                        )
            else:
                ours, generation_match_precondition = IpswArtifactDb(), 0

            ours.upsert(meta_key, ipsw_meta)
            try:
                blob.upload_from_string(
                    ours.model_dump_json(),
                    if_generation_match=generation_match_precondition,
                )
                return ours
            except PreconditionFailed:
                retry = retry - 1

        raise RuntimeError("Failed to update meta-data item")


def import_meta_from_appledb(storage: GoogleStorage) -> None:
    with tempfile.TemporaryDirectory() as processing_dir:
        processing_dir_path = Path(processing_dir)

        ipsw_storage = IpswGcsStorage(processing_dir_path, storage.bucket)
        artifacts_meta_blob = ipsw_storage.load_artifacts_meta()
        import_state_blob = ipsw_storage.load_import_state()

        AppleDbIpswImport(processing_dir_path).run()

        ipsw_storage.store_artifacts_meta(artifacts_meta_blob)
        ipsw_storage.store_import_state(import_state_blob)


def ipsw_meta_sort_key(artifact: IpswArtifact) -> datetime.date:
    if artifact.released:
        return artifact.released
    else:
        return datetime.date(datetime.MINYEAR, 1, 1)


def mirror(storage: GoogleStorage) -> None:
    mirror_size = 0
    artifacts_with_source_without_size = 0
    artifact_sources = 0

    with tempfile.TemporaryDirectory() as processing_dir:
        processing_dir_path = Path(processing_dir)

        ipsw_storage = IpswGcsStorage(processing_dir_path, storage.bucket)
        blob = ipsw_storage.load_artifacts_meta()
        if not blob.exists():
            logger.error("Cannot mirror without IPSW meta-data on GCS")
            return

        try:
            fp = open(ipsw_storage.local_artifacts_meta)
        except IOError:
            logger.error("Failed to load IPSW meta-data")
        else:
            with fp:
                ipsw_meta = IpswArtifactDb.model_validate_json(fp.read())
                filtered_artifacts = [
                    artifact
                    for artifact in ipsw_meta.artifacts.values()
                    if artifact.released is not None
                    and artifact.released.year >= datetime.date.today().year - 1
                ]
                logger.info(f"Number of filtered artifacts = {len(filtered_artifacts)}")
                for artifact in sorted(
                    filtered_artifacts, key=ipsw_meta_sort_key, reverse=True
                ):
                    for source in artifact.sources:
                        logger.info(
                            f"\t/mirror/ipsw/{artifact.platform}/{artifact.version}/{artifact.build}/{source.file_name}"
                        )
                        artifact_sources += 1
                        if source.size:
                            mirror_size += source.size
                        else:
                            logger.warning(
                                f"f{artifact} has source {source} without size"
                            )
                            artifacts_with_source_without_size += 1
    logger.info(f"artifact-sources = {artifact_sources}")
    logger.info(f"mirror-size = {mirror_size // 1024 // 1024 // 1024}GiB")
    logger.info(
        f"artifact-sources w/o size property = {artifacts_with_source_without_size}"
    )
