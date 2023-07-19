import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from math import floor
from pathlib import Path
from typing import Any

import requests
import sentry_sdk


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


class ArtifactProcessingState(StrEnum):
    # we retrieved metadata from apple and merged it with ours
    INDEXED = "indexed"

    # beta and normal releases are often the exact same file and don't need to be stored or processed twice
    INDEXED_DUPLICATE = "indexed_duplicate"

    # we mirrored that artifact, and it is ready for further processing
    MIRRORED = "mirrored"

    # we failed to retrieve or upload the artifact (artifacts can get unreachable)
    MIRRORING_FAILED = "mirroring_failed"

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

    return sha1sum.hexdigest() == hash_sum


def download_url_to_file(url: str, filepath: Path) -> None:
    res = requests.get(url, stream=True)
    content_length = res.headers.get("content-length")
    if not content_length:
        raise RuntimeError("URL endpoint does not respond with a content-length header")

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
