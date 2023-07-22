import datetime
import logging
import time
from pathlib import Path

import sentry_sdk
from google.cloud.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket, Client  # type: ignore[import]

from symx._common import (
    ArtifactProcessingState,
    download_url_to_file,
    compare_md5_hash,
)
from symx._ipsw.common import (
    ARTIFACTS_META_JSON,
    IpswArtifactDb,
    IpswArtifact,
    IpswSource,
)
from symx._ipsw.meta_sync.appledb import IMPORT_STATE_JSON
from symx._ipsw.mirror import verify_download

logger = logging.getLogger(__name__)


class IpswGcsStorage:
    def __init__(self, local_dir: Path, project: str | None, bucket: str) -> None:
        self.local_dir = local_dir
        self.local_artifacts_meta = self.local_dir / ARTIFACTS_META_JSON
        self.local_import_state = self.local_dir / IMPORT_STATE_JSON
        self.project = project
        self.client: Client = Client(project=self.project)
        self.bucket: Bucket = self.client.bucket(bucket)

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

    def mirror_ipsw_from_apple(
        self,
        artifact: IpswArtifact,
        download_dir: Path,
        start: float,
        timeout: datetime.timedelta,
    ) -> None:
        logger.info(f"Downloading {artifact}")
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
        for source in artifact.sources:
            if int(time.time() - start) > timeout.seconds:
                return

            sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
            if source.processing_state not in {
                ArtifactProcessingState.INDEXED,
                ArtifactProcessingState.MIRRORING_FAILED,
            }:
                logger.info(f"Bypassing {source.link} because it was already mirrored")
                continue

            filepath = download_dir / source.file_name
            download_url_to_file(str(source.link), filepath)
            if not verify_download(filepath, source):
                continue

            updated_artifact = self.upload_ipsw(artifact, (filepath, source))
            self.update_meta_item(updated_artifact)
            filepath.unlink()

    def upload_ipsw(
        self, artifact: IpswArtifact, downloaded_source: tuple[Path, IpswSource]
    ) -> IpswArtifact:
        ipsw_file, source = downloaded_source
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
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
            if not compare_md5_hash(ipsw_file, blob):
                logger.error(
                    "Trying to upload IPSW that already exists in mirror with a"
                    " different MD5"
                )
                artifact.sources[source_idx].processing_state = (
                    ArtifactProcessingState.MIRRORING_FAILED
                )
                return artifact
        else:
            # this file will be split into considerable chunks: set timeout to something high
            blob.upload_from_filename(str(ipsw_file), timeout=3600)
            logger.info("Upload finished. Updating IPSW meta-data.")

        artifact.sources[source_idx].mirror_path = mirror_filename
        artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORED
        artifact.sources[source_idx].update_last_run()

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
