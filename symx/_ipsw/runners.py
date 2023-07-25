import datetime
import logging
import time

import sentry_sdk

from symx._common import ArtifactProcessingState, download_url_to_file
from symx._ipsw.meta_sync.appledb import AppleDbIpswImport
from symx._ipsw.mirror import verify_download
from symx._ipsw.storage.gcs import IpswGcsStorage

logger = logging.getLogger(__name__)


def import_meta_from_appledb(ipsw_storage: IpswGcsStorage) -> None:
    artifacts_meta_blob = ipsw_storage.load_artifacts_meta()
    import_state_blob = ipsw_storage.load_import_state()

    AppleDbIpswImport(ipsw_storage.local_dir).run()

    ipsw_storage.store_artifacts_meta(artifacts_meta_blob)
    ipsw_storage.store_import_state(import_state_blob)


def mirror(ipsw_storage: IpswGcsStorage, timeout: datetime.timedelta) -> None:
    start = time.time()
    for artifact in ipsw_storage.indexed_iter():
        if int(time.time() - start) > timeout.seconds:
            logger.warning(f"Exiting IPSW mirror due to elapsed timeout of {timeout}")
            return

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

            filepath = ipsw_storage.local_dir / source.file_name
            download_url_to_file(str(source.link), filepath)
            if not verify_download(filepath, source):
                continue

            updated_artifact = ipsw_storage.upload_ipsw(artifact, (filepath, source))
            ipsw_storage.update_meta_item(updated_artifact)
            filepath.unlink()


def extract_runner(
    storage_backend: IpswGcsStorage, timeout: datetime.timedelta
) -> None:
    pass
