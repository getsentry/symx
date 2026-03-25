import datetime
import logging
import shutil
from pathlib import Path
from collections.abc import Callable, Iterable, Iterator, Sequence

import sentry_sdk
import sentry_sdk.metrics
from google.cloud.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket, Client

from symx.model import ArtifactProcessingState
from symx.gcs import (
    SYMX_GCS_RETRY,
    compare_md5_hash,
    try_download_to_filename,
    upload_symbol_binaries,
)
from symx.ipsw.model import (
    ARTIFACTS_META_JSON,
    IpswArtifactDb,
    IpswArtifact,
    IpswSource,
)
from symx.ipsw.mirror import verify_download
from symx.ipsw.storage import IpswStorage

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
        if any(source.processing_state == ArtifactProcessingState.MIRRORED for source in artifact.sources)
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
        and any(source.processing_state == ArtifactProcessingState.INDEXED for source in artifact.sources)
    ]


class IpswGcsStorage(IpswStorage):
    def __init__(self, local_dir: Path, project: str | None, bucket: str) -> None:
        self.local_dir = local_dir
        self.local_artifacts_meta = self.local_dir / ARTIFACTS_META_JSON
        self.project = project
        self.client: Client = Client(project=self.project)
        self.bucket: Bucket = self.client.bucket(bucket)

    def load_artifacts_meta(self) -> Blob:
        artifacts_meta_blob = self.bucket.blob(ARTIFACTS_META_JSON)
        if artifacts_meta_blob.exists():
            artifacts_meta_blob.download_to_filename(str(self.local_artifacts_meta))
        return artifacts_meta_blob

    def store_artifacts_meta(self, artifacts_meta_blob: Blob) -> None:
        artifacts_meta_blob.upload_from_filename(
            str(self.local_artifacts_meta),
            if_generation_match=artifacts_meta_blob.generation,
        )

    def upload_ipsw(self, artifact: IpswArtifact, downloaded_source: tuple[Path, IpswSource]) -> IpswArtifact:
        ipsw_file, source = downloaded_source
        source_idx = artifact.sources.index(source)
        if not ipsw_file.is_file():
            raise RuntimeError("Path to upload must be a file")

        with sentry_sdk.start_span(op="gcs.upload_ipsw", name=f"Upload IPSW {source.file_name}") as span:
            file_size = ipsw_file.stat().st_size
            span.set_data("file_name", source.file_name)
            span.set_data("file_size_bytes", file_size)
            span.set_data("artifact_key", artifact.key)

            logger.info("Uploading IPSW %s (%dMiB) to %s", ipsw_file.name, file_size // (1024 * 1024), self.bucket.name)

            mirror_filename = f"mirror/ipsw/{artifact.platform}/{artifact.version}/{artifact.build}/{source.file_name}"
            blob = self.bucket.blob(mirror_filename)
            if blob.exists():
                # if the existing remote file has the same MD5 hash as the file we are about to upload, we can go on
                # without uploading and only update meta, since that means some meta is still set to INDEXED instead
                # of MIRRORED. On the other hand, if the hashes differ, then we have a problem and should be getting out
                if not compare_md5_hash(ipsw_file, blob):
                    logger.error("Trying to upload IPSW that already exists in mirror with a different MD5")
                    artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORING_FAILED
                    span.set_status("internal_error")
                    return artifact
            else:
                # this file will be split into considerable chunks: set timeout to something high
                blob.upload_from_filename(str(ipsw_file), timeout=3600, retry=SYMX_GCS_RETRY)
                logger.info("Upload finished for %s", source.file_name)
                sentry_sdk.metrics.distribution(
                    "gcs.upload.size_bytes", file_size, unit="byte", attributes={"type": "ipsw"}
                )

            artifact.sources[source_idx].mirror_path = mirror_filename
            artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORED
            artifact.sources[source_idx].update_last_run()

        return artifact

    def update_meta_item(self, ipsw_meta: IpswArtifact, retry: int = 5) -> IpswArtifactDb:
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

    def refresh_artifacts_db(self) -> tuple[Blob, IpswArtifactDb, int]:
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
            _, meta_db, _ = self.refresh_artifacts_db()
            if not meta_db.artifacts:
                logger.error("No artifacts in IPSW meta-data.")
                return

            filtered_artifacts = filter_fun(meta_db.artifacts.values())
            logger.info("Number of filtered artifacts:", extra={"filtered_artifacts": len(filtered_artifacts)})
            sorted_by_age_descending = sorted(filtered_artifacts, key=_ipsw_artifact_sort_by_released, reverse=True)

            if not sorted_by_age_descending:
                break

            yield sorted_by_age_descending[0]

    def download_ipsw(self, ipsw_source: IpswSource) -> Path | None:
        with sentry_sdk.start_span(op="gcs.download_ipsw", name=f"Download IPSW {ipsw_source.file_name}") as span:
            span.set_data("file_name", ipsw_source.file_name)
            span.set_data("mirror_path", ipsw_source.mirror_path)

            logger.info("Downloading IPSW %s from mirror", ipsw_source.file_name)
            if ipsw_source.mirror_path is None:
                logger.error("Attempting to download IPSW without mirror path")
                span.set_status("invalid_argument")
                return None

            blob = self.bucket.blob(ipsw_source.mirror_path)
            local_ipsw_path = self.local_dir / ipsw_source.file_name
            if not blob.exists():
                logger.error("IPSW mirror path no longer accessible: %s", ipsw_source.mirror_path)
                span.set_status("not_found")
                return None

            if not (try_download_to_filename(blob, local_ipsw_path) and verify_download(local_ipsw_path, ipsw_source)):
                span.set_status("internal_error")
                return None

            if local_ipsw_path.exists():
                span.set_data("downloaded_bytes", local_ipsw_path.stat().st_size)
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
        artifact.sources[source_idx].processing_state = ArtifactProcessingState.SYMBOLS_EXTRACTED
        artifact.sources[source_idx].update_last_run()
        self.update_meta_item(artifact)

    def clean_local_dir(self) -> None:
        for item in self.local_dir.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item)
                    logger.info("Removed directory as part of local storage cleanup", extra={"directory": item})
                except Exception as e:
                    logger.error("Error occurred while removing directory.", extra={"directory": item, "exception": e})
            elif item.is_file() and item.suffix == ".ipsw":
                try:
                    item.unlink()
                    logger.info("Removed file as part of local storage cleanup.", extra={"file": item})
                except Exception as e:
                    logger.error("Error occurred while removing file.", extra={"file": item, "exception": e})
