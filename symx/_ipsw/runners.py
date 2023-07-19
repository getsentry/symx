import datetime
import logging
import tempfile
from pathlib import Path

import sentry_sdk
from google.cloud.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket  # type: ignore[import]

from symx._common import ArtifactProcessingState
from symx._gcs import GoogleStorage, _compare_md5_hash
from symx._ipsw.common import (
    ARTIFACTS_META_JSON,
    IpswArtifactDb,
    IpswArtifact,
    IpswSource,
)
from symx._ipsw.meta_sync.appledb import AppleDbIpswImport, IMPORT_STATE_JSON
from symx._ipsw.mirror import download_ipsw_from_apple

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

    def upload_ipsw(
        self, artifact: IpswArtifact, downloaded_files: list[tuple[Path, IpswSource]]
    ) -> IpswArtifact:
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
        for ipsw_file, source in downloaded_files:
            sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
            source_idx = artifact.sources.index(source)
            if not ipsw_file.is_file():
                raise RuntimeError("Path to upload must be a file")

            logger.info(f"Start uploading {ipsw_file.name} to {self.bucket.name}")

            mirror_filename = f"mirror/ipsw/{artifact.platform}/{artifact.version}/{artifact.build}/{source.file_name}"
            blob = self.bucket.blob(mirror_filename)
            if blob.exists():
                # if the existing remote file has the same MD5 hash as the file we are about to upload, we can go on
                # without uploading and only update meta, since that means some meta is still set to INDEXED instead
                # of MIRRORED. On the other hand, if the hashes differ, then we have a problem and should be getting out
                if not _compare_md5_hash(ipsw_file, blob):
                    logger.error(
                        "Trying to upload IPSW that already exists in mirror with a"
                        " different MD5"
                    )
                    artifact.sources[source_idx].processing_state = (
                        ArtifactProcessingState.MIRRORING_FAILED
                    )
                    continue
            else:
                # this file will be split into considerable chunks: set timeout to something high
                blob.upload_from_filename(str(ipsw_file), timeout=3600)
                logger.info("Upload finished. Updating OTA meta-data.")

            artifact.sources[source_idx].download_path = mirror_filename
            artifact.sources[source_idx].processing_state = (
                ArtifactProcessingState.MIRRORED
            )

        # not sure about this, but I guess it is okay make the artifact available to the extractor
        # if we mirrored some of its sources successfully
        if any(
            source.processing_state == ArtifactProcessingState.MIRRORED
            for source in artifact.sources
        ):
            artifact.processing_state = ArtifactProcessingState.MIRRORED

        artifact.update_last_run()

        return artifact

    def update_meta_item(self, ipsw_meta: IpswArtifact) -> IpswArtifactDb:
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

            ours.upsert(ipsw_meta.key, ipsw_meta)
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
    with tempfile.TemporaryDirectory() as processing_dir:
        processing_dir_path = Path(processing_dir)

        ipsw_storage = IpswGcsStorage(processing_dir_path, storage.bucket)
        blob = ipsw_storage.load_artifacts_meta()
        if not blob.exists():
            logger.error("Cannot mirror without IPSW meta-data on GCS")
            return

        ipsw_meta = _load_local_ipsw_meta(ipsw_storage)
        if ipsw_meta is None:
            return

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
            downloaded_files = download_ipsw_from_apple(artifact, processing_dir_path)
            updated_artifact = ipsw_storage.upload_ipsw(artifact, downloaded_files)
            ipsw_storage.update_meta_item(updated_artifact)


def _load_local_ipsw_meta(ipsw_storage: IpswGcsStorage) -> IpswArtifactDb | None:
    try:
        fp = open(ipsw_storage.local_artifacts_meta)
    except IOError:
        logger.error("Failed to load IPSW meta-data")
    else:
        with fp:
            return IpswArtifactDb.model_validate_json(fp.read())

    return None
