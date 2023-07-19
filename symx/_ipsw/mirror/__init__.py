import logging
from pathlib import Path

import sentry_sdk

from symx._common import (
    check_sha1,
    ArtifactProcessingState,
    download_url_to_file,
)
from symx._ipsw.common import IpswArtifact

logger = logging.getLogger(__name__)


def download_ipsw_from_apple(ipsw_meta: IpswArtifact, download_dir: Path) -> list[Path]:
    logger.info(f"Downloading {ipsw_meta}")
    sentry_sdk.set_tag("ipsw.artifact.key", ipsw_meta.key)
    download_paths = []
    for source in ipsw_meta.sources:
        sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
        filepath = download_dir / source.file_name
        download_url_to_file(str(source.link), filepath)

        if source.hashes and source.hashes.sha1:
            # if we have a hash-sum in the meta-data, let's verify the download against it
            if check_sha1(source.hashes.sha1, filepath):
                logger.info(f"Downloading {ipsw_meta} completed and SHA-1 verified")
                download_paths.append(filepath)
            else:
                source.processing_state = ArtifactProcessingState.MIRRORING_FAILED
                logger.error(
                    "Could not verify downloaded IPSW with its meta-data hash."
                )
        elif source.size:
            # if we only have a size in the meta-data, let's test if the download has that size
            actual_size = filepath.stat().st_size
            if actual_size == source.size:
                logger.info(
                    f"Downloading {source.link} completed but only size verified (no"
                    " hash in meta-data)"
                )
                download_paths.append(filepath)
            else:
                source.processing_state = ArtifactProcessingState.MIRRORING_FAILED
                logger.error(
                    f"The size of the downloaded IPSW (= {actual_size}bytes) is"
                    f" different from the one its meta-data (= {source.size}bytes)."
                )
        else:
            # if we have neither size nor hash-sum, we can only accept the download as is
            logger.info(
                f"Downloading {source.link} completed but only size verified (no hash"
                " in meta-data)"
            )
            download_paths.append(filepath)

    return download_paths
