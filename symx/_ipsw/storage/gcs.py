import datetime
import logging
import shutil
from pathlib import Path
from typing import Tuple, Iterator, Iterable, Callable, Sequence

import sentry_sdk
from google.cloud.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket, Client  # type: ignore[import-untyped]

from symx._common import (
    ArtifactProcessingState,
    compare_md5_hash,
    upload_symbol_binaries,
    try_download_to_filename,
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


def _ipsw_artifact_sort_by_released(artifact: IpswArtifact) -> datetime.date:
    if artifact.released:
        return artifact.released
    else:
        return datetime.date(datetime.MINYEAR, 1, 1)


def extract_filter(
    artifacts: Iterable[IpswArtifact],
) -> Sequence[IpswArtifact]:
    # we can extract from any source that has been mirrored
    return [
        artifact
        for artifact in artifacts
        if any(
            source.processing_state == ArtifactProcessingState.MIRRORED
            for source in artifact.sources
        )
    ]


def mirror_filter(
    artifacts: Iterable[IpswArtifact],
) -> Sequence[IpswArtifact]:
    # to mirror, we want all artifacts...
    # - that have a release date within this and the previous year and
    # - where some of its sources are still indexed
    return [
        artifact
        for artifact in artifacts
        if artifact.released is not None
        and artifact.released.year >= datetime.date.today().year - 1
        and any(
            source.processing_state == ArtifactProcessingState.INDEXED
            for source in artifact.sources
        )
    ]


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
            blob.upload_from_filename(str(ipsw_file), timeout=3600, num_retries=10)
            logger.info("Upload finished. Updating IPSW meta-data.")

        artifact.sources[source_idx].mirror_path = mirror_filename
        artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORED
        artifact.sources[source_idx].update_last_run()

        return artifact

    def update_meta_item(
        self, ipsw_meta: IpswArtifact, retry: int = 5
    ) -> IpswArtifactDb:
        while retry > 0:
            blob, meta_db, generation = self.refresh_artifacts_db()
            meta_db.upsert(ipsw_meta.key, ipsw_meta)
            try:
                blob.upload_from_string(
                    meta_db.model_dump_json(),
                    if_generation_match=generation,
                )
                return meta_db
            except PreconditionFailed:
                retry = retry - 1

        raise RuntimeError("Failed to update meta-data item")

    def refresh_artifacts_db(self) -> Tuple[Blob, IpswArtifactDb, int]:
        blob = self.load_artifacts_meta()
        if blob.exists():
            try:
                fp = open(self.local_artifacts_meta)
            except IOError:
                meta_db, generation = IpswArtifactDb(), 0
            else:
                with fp:
                    meta_db, generation = (
                        IpswArtifactDb.model_validate_json(fp.read()),
                        blob.generation,
                    )
        else:
            meta_db, generation = IpswArtifactDb(), 0

        if generation is None:
            generation = 0
        return blob, meta_db, generation

    def artifact_iter(
        self, filter_fun: Callable[[Iterable[IpswArtifact]], Sequence[IpswArtifact]]
    ) -> Iterator[IpswArtifact]:
        """
        This iterator refreshes the database with each yield. So if you do not change the (remote) data during the loop
        it will return the same item every time. Specifically the iter is meant to enable processing that results in a
        state-change of the artifacts sources.

        If you don't do that, don't use it. There is nothing wrong with iterating an offline-version of the meta-data if
        the processing is offline and doesn't need to interact with other (live) workflows.

        Using this iter is the opposite and allows us to work with the latest data and update meta-data to the latest
        state within the context of concurrent long-running workflows.
        :param filter_fun: a callable that expects some artifacts and returns a filtered list based on some condition
        """
        while True:
            meta_blob, meta_db, generation = self.refresh_artifacts_db()
            if len(meta_db.artifacts) == 0:
                logger.error("No artifacts in IPSW meta-data.")
                return

            filtered_artifacts = filter_fun(meta_db.artifacts.values())
            logger.info(f"Number of filtered artifacts = {len(filtered_artifacts)}")
            sorted_by_age_descending = sorted(
                filtered_artifacts, key=_ipsw_artifact_sort_by_released, reverse=True
            )

            if len(sorted_by_age_descending) == 0:
                break

            yield sorted_by_age_descending[0]

    def download_ipsw(self, ipsw_source: IpswSource) -> Path | None:
        logger.info(f"Downloading source {ipsw_source.file_name}")
        blob = self.bucket.blob(ipsw_source.mirror_path)
        local_ipsw_path = self.local_dir / ipsw_source.file_name
        if not blob.exists():
            logger.error(
                "The IPSW-source references a mirror-path that is no longer accessible"
            )
            return None

        if not (
            try_download_to_filename(blob, local_ipsw_path)
            and verify_download(local_ipsw_path, ipsw_source)
        ):
            return None

        return local_ipsw_path

    def upload_symbols(
        self,
        prefix: str,
        bundle_id: str,
        artifact: IpswArtifact,
        source_idx: int,
        binary_dir: Path,
    ) -> None:
        upload_symbol_binaries(self.bucket, prefix, bundle_id, binary_dir)
        artifact.sources[source_idx].processing_state = (
            ArtifactProcessingState.SYMBOLS_EXTRACTED
        )
        artifact.sources[source_idx].update_last_run()
        self.update_meta_item(artifact)

    def clean_local_dir(self) -> None:
        for item in self.local_dir.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item)
                    logger.info(
                        f"Removed directory {item} as part of local storage cleanup"
                    )
                except Exception as e:
                    logger.error(
                        f"Error occurred while removing directory: {item}, Error: {e}"
                    )
            elif item.is_file() and item.suffix == ".ipsw":
                try:
                    item.unlink()
                    logger.info(f"Removed {item} as part of local storage cleanup")
                except Exception as e:
                    logger.error(
                        f"Error occurred while removing directory: {item}, Error: {e}"
                    )
