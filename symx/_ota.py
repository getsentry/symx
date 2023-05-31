import datetime
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from math import floor
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

MiB = 1024 * 1024

logger = logging.getLogger(__name__)

PLATFORMS = [
    "ios",
    "watchos",
    "tvos",
    "audioos",
    "accessory",
    "macos",
    "recovery",
]

ARTIFACTS_META_JSON = "ota_image_meta.json"


class OtaProcessingState(str, Enum):
    # we retrieved metadata from apple and merged it with ours
    INDEXED = "indexed"

    # beta and normal releases are often the exact same file and don't need to be stored or processed twice
    INDEXED_DUPLICATE = "indexed_duplicate"

    # we mirrored that artifact, and it is ready for further processing
    MIRRORED = "mirrored"

    # we failed to retrieve or upload the artifact (OTAs can get unreachable)
    MIRRORING_FAILED = "mirroring_failed"

    # we stored the extracted dyld_shared_cache (optimization, not implemented yet)
    DSC_EXTRACTED = "dsc_extracted"

    # there was no dyld_shared_cache in the OTA, because it was a partial update
    DSC_EXTRACTION_FAILED = "dsc_extraction_failed"

    # the symx goal: symbols are stored for symbolicator to grab
    SYMBOLS_EXTRACTED = "symbols_extracted"

    # this would typically happen when we want to update the symbol store from a given image atomically,
    # and it turns out there are debug-ids already present but with different hash or something similar.
    SYMBOL_EXTRACTION_FAILED = "symbol_extraction_failed"

    # manually assigned to ignore artifact from any processing
    IGNORED = "ignored"


@dataclass
class OtaArtifact:
    build: str
    description: List[str]
    version: str
    platform: str
    id: str
    url: str
    download_path: Optional[str]
    devices: List[str]
    hash: str
    hash_algorithm: str
    last_run: int = 0  # currently the run_id of the GHA Workflow so we can look it up
    processing_state: OtaProcessingState = OtaProcessingState.INDEXED

    def is_indexed(self) -> bool:
        return self.processing_state == OtaProcessingState.INDEXED


OtaMetaData = dict[str, OtaArtifact]


def parse_download_meta_output(
    platform: str,
    result: subprocess.CompletedProcess[bytes],
    meta_data: OtaMetaData,
    beta: bool,
) -> None:
    if result.returncode != 0:
        logger.error(f"Error: {result.stderr!r}")
    else:
        platform_meta = json.loads(result.stdout)
        for meta_item in platform_meta:
            url = meta_item["url"]
            zip_id = url[url.rfind("/") + 1 : -4]
            if len(zip_id) != 40:
                raise RuntimeError(f"Unexpected url-format in {meta_item}")

            if "description" in meta_item:
                desc = [meta_item["description"]]
            else:
                desc = []

            if beta:
                # betas can have the same zip-id as later releases, often with the same contents
                # they only differ by the build. we need to tag them in the key, and we should add
                # a state INDEXED_DUPLICATE as to not process them twice.
                key = zip_id + "_beta"
            else:
                key = zip_id

            meta_data[key] = OtaArtifact(
                id=zip_id,
                build=meta_item["build"],
                description=desc,
                version=meta_item["version"],
                platform=platform,
                url=url,
                devices=meta_item.get("devices"),
                download_path=None,
                hash=meta_item["hash"],
                hash_algorithm=meta_item["hash_algorithm"],
                processing_state=OtaProcessingState.INDEXED,
                last_run=int(os.getenv("GITHUB_RUN_ID", 0)),
            )


def retrieve_current_meta() -> OtaMetaData:
    meta: OtaMetaData = {}
    for platform in PLATFORMS:
        logger.info(f"Downloading meta for {platform}")
        cmd = [
            "ipsw",
            "download",
            "ota",
            "--platform",
            platform,
            "--urls",
            "--json",
        ]

        parse_download_meta_output(
            platform, subprocess.run(cmd, capture_output=True), meta, False
        )

        beta_cmd = cmd.copy()
        beta_cmd.append("--beta")
        parse_download_meta_output(
            platform, subprocess.run(beta_cmd, capture_output=True), meta, True
        )

    return meta


def merge_lists(a: List[str], b: List[str]) -> List[str]:
    return list(set(a + b))


def merge_meta_data(ours: OtaMetaData, theirs: OtaMetaData) -> None:
    for their_key, their_item in theirs.items():
        if their_key in ours.keys():
            # we already have that id in out meta-store
            our_item = ours[their_key]

            # merge data that can change over time but has no effect on the identity of the artifact
            ours[their_key].description = merge_lists(
                our_item.description, their_item.description
            )
            ours[their_key].devices = merge_lists(our_item.devices, their_item.devices)

            # this is a little bit the core of the whole thing:
            # - what does apple consider identity?
            # - what is sufficient for sentry?
            # - how to migrate if identities change?
            if not (
                their_item.build == our_item.build
                and their_item.version == our_item.version
                and their_item.platform == our_item.platform
                and their_item.url == our_item.url
                and their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
            ):
                raise RuntimeError(
                    f"Same matching keys with different value:\n\tlocal: {our_item}\n\tapple: {their_item}"
                )
        else:
            # it is a new key, store their item in our store
            ours[their_key] = their_item

            # identify and mark beta <-> normal release duplicates
            for our_k, our_v in ours.items():
                if (
                    their_item.hash == our_v.hash
                    and their_item.hash_algorithm == our_v.hash_algorithm
                    and their_item.platform == our_v.platform
                    and their_item.version == our_v.version
                    and their_item.build != our_v.build
                ):
                    ours[
                        their_key
                    ].processing_state = OtaProcessingState.INDEXED_DUPLICATE


def check_hash(ota_meta: OtaArtifact, filepath: Path) -> bool:
    if ota_meta.hash_algorithm != "SHA-1":
        raise RuntimeError(f"Unexpected hash-algo: {ota_meta.hash_algorithm}")

    sha1sum = hashlib.sha1()
    with open(filepath, "rb") as f:
        block = f.read(2**16)
        while len(block) != 0:
            sha1sum.update(block)
            block = f.read(2**16)

    return sha1sum.hexdigest() == ota_meta.hash


def download_ota(ota_meta: OtaArtifact, download_dir: Path) -> Path:
    logger.info(f"Downloading {ota_meta}")

    res = requests.get(ota_meta.url, stream=True)
    content_length = res.headers.get("content-length")
    if not content_length:
        raise RuntimeError("OTA Url does not respond with a content-length header")

    total = int(content_length)
    total_mib = total / MiB
    logger.debug(f"OTA Filesize: {floor(total_mib)} MiB")

    # TODO: how much prefix for identity?
    filepath = (
        download_dir
        / f"{ota_meta.platform}_{ota_meta.version}_{ota_meta.build}_{ota_meta.id}.zip"
    )
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
    if check_hash(ota_meta, filepath):
        logger.info(f"Downloading {ota_meta} completed")
        return filepath

    raise RuntimeError("Failed to download")


class Ota:
    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self.meta: Dict[Any, Any] = {}

    def update_meta(self) -> None:
        logger.debug("Updating OTA meta-data")
        apple_meta = retrieve_current_meta()
        self.meta = self.storage.save_meta(apple_meta)

    def mirror(self, timeout: datetime.timedelta) -> None:
        logger.debug(f"Mirroring OTA images to {self.storage.bucket.name}")

        start = time.time()
        self.update_meta()
        with tempfile.TemporaryDirectory() as download_dir:
            key: str
            ota: OtaArtifact
            for key, ota in self.meta.items():
                if int(time.time() - start) > timeout.seconds:
                    logger.info(
                        f"Exiting OTA mirror due to elapsed timeout of {timeout}"
                    )
                    return

                if not ota.is_indexed():
                    continue

                ota_file = download_ota(ota, Path(download_dir))
                self.storage.save_ota(ota, ota_file)
                ota_file.unlink()
