"""OTA download from Apple and hash verification."""

import logging
from pathlib import Path

import sentry_sdk
import sentry_sdk.metrics

from symx.common import check_sha1, try_download_url_to_file
from symx.ota.common import OtaArtifact

logger = logging.getLogger(__name__)


def check_ota_hash(ota_meta: OtaArtifact, filepath: Path) -> bool:
    if ota_meta.hash_algorithm != "SHA-1":
        raise RuntimeError(f"Unexpected hash-algo: {ota_meta.hash_algorithm}")

    return check_sha1(ota_meta.hash, filepath)


def download_ota_from_apple(ota_meta: OtaArtifact, download_dir: Path) -> Path:
    with sentry_sdk.start_span(
        op="http.download",
        name=f"Download OTA {ota_meta.platform} {ota_meta.version} {ota_meta.build}",
    ) as span:
        span.set_data("platform", ota_meta.platform)
        span.set_data("version", ota_meta.version)
        span.set_data("build", ota_meta.build)

        logger.info("Downloading OTA %s %s %s from Apple", ota_meta.platform, ota_meta.version, ota_meta.build)

        filepath = download_dir / f"{ota_meta.platform}_{ota_meta.version}_{ota_meta.build}_{ota_meta.id}.zip"
        try_download_url_to_file(ota_meta.url, filepath)
        if check_ota_hash(ota_meta, filepath):
            if filepath.exists():
                size = filepath.stat().st_size
                span.set_data("downloaded_bytes", size)
                sentry_sdk.metrics.distribution(
                    "ota.download.size_bytes", size, unit="byte", attributes={"platform": ota_meta.platform}
                )
            logger.info(
                "OTA download completed and verified for %s %s %s", ota_meta.platform, ota_meta.version, ota_meta.build
            )
            return filepath

    raise RuntimeError(f"Failed to download {ota_meta.url}")
