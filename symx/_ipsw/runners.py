import datetime
import logging
import time

from symx._common import (
    ArtifactProcessingState,
)
from symx._ipsw.common import (
    IpswArtifactDb,
    IpswArtifact,
)
from symx._ipsw.meta_sync.appledb import AppleDbIpswImport
from symx._ipsw.storage.gcs import IpswGcsStorage

logger = logging.getLogger(__name__)


def import_meta_from_appledb(ipsw_storage: IpswGcsStorage) -> None:
    artifacts_meta_blob = ipsw_storage.load_artifacts_meta()
    import_state_blob = ipsw_storage.load_import_state()

    AppleDbIpswImport(ipsw_storage.local_dir).run()

    ipsw_storage.store_artifacts_meta(artifacts_meta_blob)
    ipsw_storage.store_import_state(import_state_blob)


def _ipsw_artifact_sort_by_released(artifact: IpswArtifact) -> datetime.date:
    if artifact.released:
        return artifact.released
    else:
        return datetime.date(datetime.MINYEAR, 1, 1)


def mirror(ipsw_storage: IpswGcsStorage, timeout: datetime.timedelta) -> None:
    blob = ipsw_storage.load_artifacts_meta()
    if not blob.exists():
        logger.error("Cannot mirror without IPSW meta-data on GCS")
        return

    ipsw_meta = _load_local_ipsw_meta(ipsw_storage)
    if ipsw_meta is None:
        return

    # we want all artifacts...
    # - that have a release date that reaches back 1 year and
    # - where some of its sources are still indexed
    filtered_artifacts = [
        artifact
        for artifact in ipsw_meta.artifacts.values()
        if artifact.released is not None
        and artifact.released.year >= datetime.date.today().year - 1
        and any(
            source.processing_state == ArtifactProcessingState.INDEXED
            for source in artifact.sources
        )
    ]
    logger.info(f"Number of filtered artifacts = {len(filtered_artifacts)}")
    sorted_by_age_descending = sorted(
        filtered_artifacts, key=_ipsw_artifact_sort_by_released, reverse=True
    )

    start = time.time()
    for artifact in sorted_by_age_descending:
        if int(time.time() - start) > timeout.seconds:
            logger.warning(f"Exiting IPSW mirror due to elapsed timeout of {timeout}")
            return
        ipsw_storage.mirror_ipsw_from_apple(artifact, ipsw_storage.local_dir)


def _load_local_ipsw_meta(ipsw_storage: IpswGcsStorage) -> IpswArtifactDb | None:
    try:
        fp = open(ipsw_storage.local_artifacts_meta)
    except IOError:
        logger.error("Failed to load IPSW meta-data")
    else:
        with fp:
            return IpswArtifactDb.model_validate_json(fp.read())

    return None
