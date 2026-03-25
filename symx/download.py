"""HTTP file download with retry logic."""

import logging
import time as time_module
from math import floor
from pathlib import Path

import requests
import sentry_sdk
import sentry_sdk.metrics

from symx.model import MiB

logger = logging.getLogger(__name__)


def try_download_url_to_file(url: str, filepath: Path, num_retries: int = 5) -> None:
    for attempt in range(num_retries):
        try:
            download_url_to_file(url, filepath)
            return
        except Exception as e:
            if attempt < num_retries - 1:
                logger.info("Download failed, retrying", extra={"url": url, "attempt": attempt + 1})
            else:
                sentry_sdk.capture_exception(e)
                logger.warning("Failed to download URL", extra={"url": url, "attempts": num_retries, "exception": e})


def download_url_to_file(url: str, filepath: Path) -> None:
    with sentry_sdk.start_span(op="http.download", name=f"Download {filepath.name}") as span:
        span.set_data("url", str(url))
        span.set_data("filepath", str(filepath))
        start = time_module.monotonic()

        res = requests.get(url, stream=True)
        content_length = res.headers.get("content-length")
        if not content_length:
            logger.warning("URL endpoint does not respond with a content-length header")
        else:
            total = int(content_length)
            total_mib = total / MiB
            logger.info("Filesize: %dMiB", floor(total_mib))
            span.set_data("content_length_bytes", total)

        with open(filepath, "wb") as f:
            actual = 0
            last_print = 0.0
            actual_mib = actual / MiB
            for chunk in res.iter_content(chunk_size=8192):
                f.write(chunk)
                actual = actual + len(chunk)

                actual_mib = actual / MiB
                if actual_mib - last_print > 100.0:
                    logger.info("%dMiB", floor(actual_mib))
                    last_print = actual_mib

            logger.info("%dMiB", floor(actual_mib))

        elapsed = time_module.monotonic() - start
        span.set_data("downloaded_bytes", actual)
        sentry_sdk.metrics.distribution("download.size_bytes", actual, unit="byte")
        sentry_sdk.metrics.distribution("download.duration_seconds", elapsed, unit="second")
