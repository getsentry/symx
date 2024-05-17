import argparse
import base64
import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from math import floor
from pathlib import Path
from typing import Any, List
from urllib.parse import ParseResult, urlparse

import requests
import sentry_sdk
from google.cloud.storage import Blob, Bucket  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

HASH_BLOCK_SIZE = 2**16

MiB = 1024 * 1024


class Arch(StrEnum):
    ARM64E = "arm64e"
    ARM64 = "arm64"
    ARM64_32 = "arm64_32"
    ARMV7 = "armv7"
    ARMV7K = "armv7k"
    ARMV7S = "armv7s"
    X86_64 = "x86_64"


class ArtifactProcessingState(StrEnum):
    # we retrieved metadata from apple and merged it with ours
    INDEXED = "indexed"

    # beta and normal releases are often the exact same file and don't need to be stored or processed twice
    INDEXED_DUPLICATE = "indexed_duplicate"

    # sometimes Apple releases an artifact that is faulty, but where they keep the meta-data available, or they remove
    # it, but we already indexed the artifact. Download or validation will fail in this case but this shouldn't fail the
    # mirroring workflow.
    INDEXED_INVALID = "indexed_invalid"

    # we mirrored that artifact, and it is ready for further processing
    MIRRORED = "mirrored"

    # we failed to retrieve or upload the artifact (artifacts can get unreachable)
    MIRRORING_FAILED = "mirroring_failed"

    # we have meta-data that points to the mirror, but the file at the path is missing or can't be validated
    MIRROR_CORRUPT = "mirror_corrupt"

    # we stored the extracted dyld_shared_cache (optimization, not implemented yet)
    DSC_EXTRACTED = "dsc_extracted"

    # there was no dyld_shared_cache in the artifact (for instance: because it was a partial update)
    DSC_EXTRACTION_FAILED = "dsc_extraction_failed"

    # the symx goal: symbols are stored for symbolicator to grab
    SYMBOLS_EXTRACTED = "symbols_extracted"

    # this would typically happen when we want to update the symbol store from a given image atomically,
    # and it turns out there are debug-ids already present but with different hash or something similar.
    SYMBOL_EXTRACTION_FAILED = "symbol_extraction_failed"

    # we already know that the bundle_id is too coarse to discriminate between sensible duplicates. we probably should
    # merge rather ignore images that result in existing bundle-ids. until this is implemented we mark images with this.
    BUNDLE_DUPLICATION_DETECTED = "bundle_duplication_detected"

    # manually assigned to ignore artifact from any processing
    IGNORED = "ignored"


@dataclass(frozen=True)
class Device:
    product: str
    model: str
    description: str
    cpu: str
    arch: Arch
    mem_class: int

    @property
    def search_name(self) -> str:
        if self.product.endswith("-A") or self.product.endswith("-B"):
            return self.product[:-2]

        return self.product


class DataClassJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


def directory_arg_type(path: str) -> Path:
    if os.path.isdir(path):
        return Path(path)

    raise ValueError(f"Error: {path} is not a valid directory")


def ipsw_version() -> str:
    result = subprocess.run(["ipsw", "version"], capture_output=True, check=True)
    output = result.stdout.decode("utf-8")
    match = re.search("Version: (.*),", output)
    if match:
        version = match.group(1)
        return version

    raise RuntimeError(f"Couldn't parse version from ipsw output: {output}")


def downloader_parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output_dir",
        dest="output_dir",
        required=True,
        type=directory_arg_type,
        help="path to the output directory where the extracted symbols are placed",
    )
    return parser.parse_args()


def downloader_validate_shell_deps() -> None:
    version = ipsw_version()
    if version:
        print(f"Using ipsw {version}")
        sentry_sdk.set_tag("ipsw.version", version)
    else:
        print("ipsw not installed")
        sys.exit(1)


DEVICE_ROW_RE = re.compile(
    r"\|\s([\w,\-]*)\s*\|\s([a-z0-9]*)\s*\|\s([\w,\-()."
    r" ]*)\s*\|\s([a-z0-9]*)\s*\|\s([a-z0-9]*)\s*\|\s(\d*)"
)


def ipsw_device_list() -> list[Device]:
    result = subprocess.run(["ipsw", "device-list"], capture_output=True, check=True)
    data_start = False
    device_list = []
    for line in result.stdout.decode("utf-8").splitlines():
        if data_start:
            match = DEVICE_ROW_RE.match(line)
            if match:
                device_list.append(
                    Device(
                        product=match.group(1),
                        model=match.group(2),
                        description=match.group(3).strip(),
                        cpu=match.group(4),
                        arch=Arch(match.group(5)),
                        mem_class=int(match.group(6)),
                    )
                )
        elif line.startswith("|--"):
            data_start = True

    return device_list


def github_run_id() -> int:
    return int(os.getenv("GITHUB_RUN_ID", 0))


def check_sha1(hash_sum: str, filepath: Path) -> bool:
    sha1sum = hashlib.sha1()
    with open(filepath, "rb") as f:
        block = f.read(HASH_BLOCK_SIZE)
        while len(block) != 0:
            sha1sum.update(block)
            block = f.read(HASH_BLOCK_SIZE)

    sha1sum_result = sha1sum.hexdigest()
    logger.debug(f"Calculated sha1 = {sha1sum_result}, expected sha1 = {hash_sum}")
    return sha1sum_result == hash_sum


def try_download_url_to_file(url: str, filepath: Path, num_retries: int = 5) -> None:
    while num_retries > 0:
        try:
            download_url_to_file(url, filepath)
            break
        except Exception as e:
            if num_retries > 0:
                num_retries = num_retries - 1
            else:
                sentry_sdk.capture_exception(e)
                logger.warning(
                    f"Failed to download URL {url} after {num_retries} retries: {e}"
                )


def download_url_to_file(url: str, filepath: Path) -> None:
    res = requests.get(url, stream=True)
    content_length = res.headers.get("content-length")
    if not content_length:
        logger.warning("URL endpoint does not respond with a content-length header")
    else:
        total = int(content_length)
        total_mib = total / MiB
        logger.debug(f"Filesize: {floor(total_mib)} MiB")

    with open(filepath, "wb") as f:
        actual = 0
        last_print = 0.0
        for chunk in res.iter_content(chunk_size=8192):
            f.write(chunk)
            actual = actual + len(chunk)

            actual_mib = actual / MiB
            if actual_mib - last_print > 100:
                logger.debug(f"{floor(actual_mib)} MiB")
                last_print = actual_mib

    logger.debug(f"{floor(actual_mib)} MiB")


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
        logger.info(
            f'"{remote_blob.name}" was already uploaded with matching MD5 hash.'
        )
        return True
    else:
        logger.error(
            f'"{remote_blob.name}" was already uploaded but MD5 hash differs from the'
            f" one uploaded (remote = {remote_hash}, local = {local_hash}). "
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
        while len(block) != 0:
            hash_md5.update(block)
            block = f.read(HASH_BLOCK_SIZE)

    return base64.b64encode(hash_md5.digest()).decode()


def parse_gcs_url(storage: str) -> ParseResult | None:
    uri = urlparse(storage)
    if uri.scheme != "gs":
        print(
            '[bold red]Unsupported "--storage" URI-scheme used:[/bold red] currently'
            ' symx supports "gs://" only'
        )
        return None

    if not uri.hostname:
        print(
            "[bold red]You must supply at least a bucket-name for the GCS storage[/bold"
            " red]"
        )
        return None
    return uri


def upload_symbol_binaries(
    bucket: Bucket, platform: str, bundle_id: str, binary_dir: Path
) -> None:
    logger.info(f"Uploading symbol binaries for {platform} and {bundle_id}")
    dest_blob_prefix = Path("symbols")
    bundle_index_path = dest_blob_prefix / platform / "bundles" / bundle_id
    blob = bucket.blob(str(bundle_index_path))
    if blob.exists():
        logger.warning(
            f"We already have a `bundle_id` {bundle_id} for {platform} in"
            " the symbol store. "
        )

    duplicate_count = 0
    new_count = 0
    data_size = 0
    new_binary_count = 0

    for root, dirs, files in os.walk(binary_dir):
        for file in files:
            local_file = Path(root) / file
            dest_blob_name = (
                dest_blob_prefix / Path(root).relative_to(binary_dir) / file
            )
            blob = bucket.blob(str(dest_blob_name))

            if blob.exists():
                # If the blob exists we can continue with the next file because there should be no duplicate
                # which contains a mismatching symbol table. this is a big assumption, and we should probably
                # cross-check the symbols between the debug-id-equal binaries of each artifact. but this if is
                # not that place.
                logger.info(
                    f"{local_file} exists in symbol-store at {dest_blob_name}. Continue"
                    " with next."
                )
                duplicate_count += 1
                continue

            blob.upload_from_filename(str(local_file), num_retries=10)
            new_count += 1
            data_size += local_file.stat().st_size
            if local_file.name == "executable":
                new_binary_count += 1
            logger.debug(f"File {local_file} uploaded to {dest_blob_name}.")

    logger.info(f"New files uploaded = {new_count}")
    logger.info(f"New binaries uploaded = {new_binary_count}")
    logger.info(f"Ignored duplicates = {duplicate_count}")
    logger.info(f"Uploaded bytes = {data_size}")


def validate_shell_deps() -> None:
    version = ipsw_version()
    if version:
        logger.info(f"Using ipsw {version}")
        sentry_sdk.set_tag("ipsw.version", version)
    else:
        logger.error("ipsw not installed")
        sys.exit(1)

    result = subprocess.run(["./symsorter", "--version"], capture_output=True)
    if result.returncode == 0:
        symsorter_version_parts = result.stdout.decode("utf-8").splitlines()
        if len(symsorter_version_parts) < 1:
            logger.error("Cannot parse symsorter version")
            sys.exit(1)

        symsorter_version = symsorter_version_parts[0].split(" ").pop()
        logger.info(f"Using symsorter {symsorter_version}")
        sentry_sdk.set_tag("symsorter.version", symsorter_version)
    else:
        symsorter_stderr = result.stderr.decode("utf-8")
        logger.error(f"symsorter failed: {symsorter_stderr}")
        sys.exit(1)


def try_download_to_filename(
    blob: Blob, local_file_path: Path, num_retries: int = 5
) -> bool:
    while num_retries > 0:
        try:
            blob.download_to_filename(str(local_file_path))
            break
        except Exception as e:
            if num_retries > 0:
                num_retries = num_retries - 1
            else:
                sentry_sdk.capture_exception(e)
                logger.warning(
                    f"Failed to download blob {blob.name} after {num_retries} retries: {e}"
                )
                return False

    return True


def is_dir_empty(dir_path: Path) -> bool:
    if dir_path.exists() and dir_path.is_dir():
        return not any(dir_path.iterdir())
    else:
        raise ValueError("The provided path does not exist or is not a directory.")


def list_dirs_in(dir_path: Path) -> List[Path]:
    if dir_path.exists() and dir_path.is_dir():
        return [entry for entry in dir_path.iterdir() if entry.is_dir()]
    else:
        raise ValueError("The provided path does not exist or is not a directory.")


def rmdir_if_exists(dir_path: Path) -> None:
    if dir_path.exists() and dir_path.is_dir():
        shutil.rmtree(dir_path)
