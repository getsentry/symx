import logging

from symx._common import ArtifactProcessingState
from symx._ota.storage.gcs import OtaGcsStorage

logger = logging.getLogger(__name__)


def migrate(storage: OtaGcsStorage) -> None:
    ota_meta = storage.load_meta()
    if ota_meta is None:
        logger.error("Could not retrieve meta-data from storage.")
        return

    for key, ota in ota_meta.artifacts.items():
        if ota.platform == "macos" and ota.processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED:
            print(f"{key}: {ota}")

            # ota.processing_state = ArtifactProcessingState.MIRRORED
            # ota.update_last_run()
            # storage.update_meta_item(key, ota)
