import logging
from pathlib import Path

from symx._common import (
    check_sha1,
    ArtifactProcessingState,
)
from symx._ipsw.common import IpswSource

logger = logging.getLogger(__name__)


def verify_download(filepath: Path, source: IpswSource) -> bool:
    if source.hashes and source.hashes.sha1:
        # if we have a hash-sum in the meta-data, let's verify the download against it
        if check_sha1(source.hashes.sha1, filepath):
            logger.info(f"Downloading {filepath.name} completed and SHA-1 verified")
            return True
        else:
            source.processing_state = ArtifactProcessingState.MIRRORING_FAILED
            logger.error("Could not verify downloaded IPSW with its meta-data hash.")
            return False
    elif source.size:
        # if we only have a size in the meta-data, let's test if the download has that size
        actual_size = filepath.stat().st_size
        if actual_size == source.size:
            logger.info(
                f"Downloading {source.link} completed but only size verified (no"
                " hash in meta-data)"
            )
            return True
        else:
            source.processing_state = ArtifactProcessingState.MIRRORING_FAILED
            logger.error(
                f"The size of the downloaded IPSW (= {actual_size}bytes) is"
                f" different from the one its meta-data (= {source.size}bytes)."
            )
            return False
    else:
        # if we have neither size nor hash-sum, we can only accept the download as is
        logger.info(
            f"Downloading {source.link} completed but not verified (no hash nor"
            " size in meta-data)"
        )
        return True
