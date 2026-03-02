import logging

from symx._common import ArtifactProcessingState
from symx._ota import parse_version_tuple
from symx._ota.storage.gcs import OtaGcsStorage

logger = logging.getLogger(__name__)


def migrate(storage: OtaGcsStorage) -> None:
    ota_meta = storage.load_meta()
    if ota_meta is None:
        logger.error("Could not retrieve meta-data from storage.")
        return

    candidates = [
        (key, ota)
        for key, ota in ota_meta.items()
        if ota.processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED and ota.platform == "watchos"
    ]
    candidates.sort(key=lambda item: parse_version_tuple(item[1].version), reverse=True)

    logger.info("Resetting failed watchOS OTAs to MIRRORED", extra={"count": len(candidates)})
    for key, ota in candidates:
        logger.info("Resetting to MIRRORED", extra={"key": key, "platform": ota.platform, "version": ota.version})
        ota.processing_state = ArtifactProcessingState.MIRRORED
        ota.update_last_run()
        storage.update_meta_item(key, ota)
