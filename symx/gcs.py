"""Google Cloud Storage utilities: upload, download, hashing, and URI parsing."""

import base64
import hashlib
import logging
import os
from contextlib import contextmanager
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import ParseResult, urlparse

import sentry_sdk
import sentry_sdk.metrics
from sentry_sdk.tracing import NoOpSpan
from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket
from google.cloud.storage.retry import DEFAULT_RETRY

from symx.model import HASH_BLOCK_SIZE

logger = logging.getLogger(__name__)

# we use the default policy (initial_wait=1s, wait_mult=2x, max_wait=60s). 300s retry timeout gives us 10 retries.
SYMX_GCS_RETRY = DEFAULT_RETRY.with_timeout(300.0)


def parse_gcs_url(storage: str) -> ParseResult | None:
    uri = urlparse(storage)
    if uri.scheme != "gs":
        print('[bold red]Unsupported "--storage" URI-scheme used:[/bold red] currently symx supports "gs://" only')
        return None

    if not uri.hostname:
        print("[bold red]You must supply at least a bucket-name for the GCS storage[/bold red]")
        return None
    return uri


def compare_md5_hash(local_file: Path, remote_blob: Blob) -> bool:
    """
    Reads the remote md5 meta from the blob and compares it with the md5 of the local file.
    :param local_file: a Path to the local file
    :param remote_blob: a loaded (!) GCS bucket blob
    :return: True if the hashes are equal, otherwise False
    """
    remote_blob.reload()
    remote_hash = remote_blob.md5_hash
    local_hash = _fs_md5_hash(local_file)
    if remote_hash == local_hash:
        logger.info("Blob was already uploaded with matching MD5 hash.", extra={"blob_name": remote_blob.name})
        return True
    else:
        logger.error(
            "Blob was already uploaded but MD5 hash differs from the one uploaded",
            extra={"blob_name": remote_blob.name, "remote_hash": remote_hash, "local_hash": local_hash},
        )
        return False


def _fs_md5_hash(file_path: Path) -> str:
    """
    GCS only stores the MD5 hash of each uploaded file, so we can't use SHA1 to compare (as we do with the meta-data
    since that is what we get from Apple to compare). Since it is still nice to quickly compare remote files without
    download we also have a local md5-hasher here.
    :param file_path:
    :return:
    """
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        block = f.read(HASH_BLOCK_SIZE)
        while block:
            hash_md5.update(block)
            block = f.read(HASH_BLOCK_SIZE)

    return base64.b64encode(hash_md5.digest()).decode()


@contextmanager
def suppress_auto_instrumented_spans() -> Generator[None, None, None]:
    """Temporarily replace the active span with a NoOpSpan to prevent auto-instrumented
    child spans (HTTP, subprocess, etc.) from being created.

    The stdlib integration creates a span for every http.client request. During bulk symbol
    uploads (5000-7000 files), this generates thousands of spans that exceed Sentry's max_spans
    limit, causing the entire transaction to be silently dropped.
    """
    scope = sentry_sdk.get_current_scope()
    original_span = scope.span
    scope.span = NoOpSpan()
    try:
        yield
    finally:
        scope.span = original_span


def _upload_file(local_file: Path, dest_blob_name: Path, bucket: Bucket) -> bool:
    blob = bucket.blob(str(dest_blob_name))

    try:
        blob.upload_from_filename(str(local_file), retry=SYMX_GCS_RETRY, if_generation_match=0)
    except PreconditionFailed:
        logger.debug(
            "Local file exists in symbol-store. Continue with next.",
            extra={"local_file": local_file, "dest_blob_name": dest_blob_name},
        )
        return False
    return True


def upload_symbol_binaries(bucket: Bucket, platform: str, bundle_id: str, binary_dir: Path) -> None:
    with sentry_sdk.start_span(op="gcs.upload_symbols", name=f"Upload symbols {platform}/{bundle_id}") as span:
        span.set_data("platform", platform)
        span.set_data("bundle_id", bundle_id)
        logger.info("Uploading symbol binaries for %s/%s", platform, bundle_id)
        dest_blob_prefix = Path("symbols")
        bundle_index_path = dest_blob_prefix / platform / "bundles" / bundle_id
        blob = bucket.blob(str(bundle_index_path))
        if blob.exists():
            logger.warning("Bundle %s already exists in symbol store for %s", bundle_id, platform)

        duplicate_count = 0
        new_count = 0
        upload_tasks: list[tuple[Path, Path, Bucket]] = []

        for root, _, files in os.walk(binary_dir):
            for file in files:
                local_file = Path(root) / file
                dest_blob_name = dest_blob_prefix / Path(root).relative_to(binary_dir) / file
                upload_tasks.append((local_file, dest_blob_name, bucket))

        span.set_data("total_files", len(upload_tasks))

        with suppress_auto_instrumented_spans(), ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(_upload_file, local_file, dest_blob_name, bucket)
                for local_file, dest_blob_name, bucket in upload_tasks
            ]

            for future in as_completed(futures):
                if not future.result():
                    duplicate_count += 1
                else:
                    new_count += 1

        logger.info(
            "Symbol upload complete: %d new, %d duplicates (total %d)", new_count, duplicate_count, len(upload_tasks)
        )
        span.set_data("new_count", new_count)
        span.set_data("duplicate_count", duplicate_count)
        sentry_sdk.metrics.distribution("symbols.uploaded_new", new_count, attributes={"platform": platform})
        sentry_sdk.metrics.distribution("symbols.duplicates", duplicate_count, attributes={"platform": platform})
        sentry_sdk.metrics.distribution("symbols.total_files", len(upload_tasks), attributes={"platform": platform})


def try_download_to_filename(blob: Blob, local_file_path: Path, num_retries: int = 5) -> bool:
    """Download a GCS blob to a local path with retries.

    Returns True only when bytes were transferred successfully. Transport integrity (md5) is
    handled by the GCS SDK internally; callers must still verify *content authenticity* against
    the authoritative upstream hash (e.g. Apple's SHA-1 for IPSW/OTA) before trusting the file.
    """
    with sentry_sdk.start_span(op="gcs.download", name=f"Download blob {blob.name}") as span:
        span.set_data("blob_name", blob.name)
        for attempt in range(num_retries):
            try:
                blob.download_to_filename(str(local_file_path))
                if local_file_path.exists():
                    size = local_file_path.stat().st_size
                    span.set_data("downloaded_bytes", size)
                    sentry_sdk.metrics.distribution("gcs.download.size_bytes", size, unit="byte")
                return True
            except Exception as e:
                if attempt < num_retries - 1:
                    logger.info(
                        "Blob download failed, retrying",
                        extra={"blob_name": blob.name, "attempt": attempt + 1},
                    )
                else:
                    sentry_sdk.capture_exception(e)
                    logger.warning(
                        "Failed to download blob.",
                        extra={"blob_name": blob.name, "attempts": num_retries, "exception": e},
                    )
                    span.set_status("internal_error")
                    return False

    return False
