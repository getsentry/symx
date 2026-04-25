"""HTTP file download with retry logic."""

import logging
import time as time_module
from collections.abc import Callable
from math import floor
from pathlib import Path

import requests
import sentry_sdk
import sentry_sdk.metrics

from symx.model import MiB

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], None]


class DownloadError(Exception):
    """Raised by `try_download_url_to_file` after all retries have failed."""


def try_download_url_to_file(
    url: str,
    filepath: Path,
    num_retries: int = 5,
    status_callback: StatusCallback | None = None,
) -> None:
    """Download `url` to `filepath`, retrying on failure.

    On total failure, raises `DownloadError` chained to the last underlying exception. Callers
    are responsible for reporting the failure to Sentry in whatever way is appropriate for their
    flow. This function intentionally does not call `capture_exception` to avoid double-reporting
    at broader `except Exception` handlers further up the call stack.
    """
    last_exc: Exception | None = None
    for attempt in range(num_retries):
        try:
            download_url_to_file(url, filepath, status_callback=status_callback)
            return
        except Exception as e:
            last_exc = e
            if attempt < num_retries - 1:
                _emit_status(
                    f"Download failed, retrying ({attempt + 1}/{num_retries}) for {url}",
                    status_callback,
                )

    _emit_status(
        f"Failed to download {url} after {num_retries} attempts",
        status_callback,
        warning=True,
    )
    raise DownloadError(f"Failed to download {url} after {num_retries} attempts") from last_exc


# (connect, read): read timeout is per-chunk, not total, so large IPSWs are fine as long as bytes keep flowing
DOWNLOAD_TIMEOUT = (10.0, 60.0)


def download_url_to_file(url: str, filepath: Path, status_callback: StatusCallback | None = None) -> None:
    with sentry_sdk.start_span(op="http.download", name=f"Download {filepath.name}") as span:
        span.set_data("url", str(url))
        span.set_data("filepath", str(filepath))
        start = time_module.monotonic()

        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as res:
            res.raise_for_status()
            content_length = res.headers.get("content-length")
            if not content_length:
                _emit_status(
                    "URL endpoint does not respond with a content-length header", status_callback, warning=True
                )
            else:
                total = int(content_length)
                total_mib = total / MiB
                _emit_status(f"Filesize: {floor(total_mib)}MiB", status_callback)
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
                        _emit_status(f"Downloaded {floor(actual_mib)}MiB", status_callback)
                        last_print = actual_mib

                _emit_status(f"Downloaded {floor(actual_mib)}MiB", status_callback)

        elapsed = time_module.monotonic() - start
        span.set_data("downloaded_bytes", actual)
        sentry_sdk.metrics.distribution("download.size_bytes", actual, unit="byte")
        sentry_sdk.metrics.distribution("download.duration_seconds", elapsed, unit="second")


def _emit_status(message: str, status_callback: StatusCallback | None, warning: bool = False) -> None:
    if status_callback is not None:
        status_callback(message)
        return
    if warning:
        logger.warning(message)
    else:
        logger.info(message)
