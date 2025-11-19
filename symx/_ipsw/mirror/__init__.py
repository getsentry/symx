import logging
from pathlib import Path

from symx._common import check_sha1
from symx._ipsw.common import IpswSource

logger = logging.getLogger(__name__)


def verify_download(filepath: Path, source: IpswSource) -> bool:
    if source.hashes and source.hashes.sha1:
        # if we have a hash-sum in the meta-data, let's verify the download against it
        if check_sha1(source.hashes.sha1, filepath):
            logger.info("Downloading completed and SHA-1 verified.", extra={"file": filepath})
            return True
        else:
            logger.error("Could not verify downloaded IPSW with its meta-data hash.")
            return False
    elif source.size:
        # if we only have a size in the meta-data, let's test if the download has that size
        actual_size = filepath.stat().st_size
        if actual_size == source.size:
            logger.info(
                "Downloading source completed but only size verified (no hash in meta-data)", extra={"source": source}
            )
            return True
        else:
            logger.error(
                "The size of the downloaded IPSW is different from the one its meta-data.",
                extra={"actual_size": actual_size, "meta_size": source.size, "source": source},
            )
            return False
    else:
        # if we have neither size nor hash-sum, we can only accept the download as is
        logger.info(
            "Downloading source completed but not verified (no hash nor size in meta-data)", extra={"source": source}
        )
        return True
