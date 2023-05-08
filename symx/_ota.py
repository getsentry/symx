import hashlib
import json
import logging
import subprocess
import tempfile

from dataclasses import dataclass
from math import floor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


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


@dataclass
class OtaArtifact:
    build: str
    description: List[str]
    version: str
    platform: str
    id: str
    url: str
    download_path: Optional[str]
    devices: Optional[List[str]]
    hash: str
    hash_algorithm: str


OtaMetaData = dict[str, OtaArtifact]


def parse_download_meta_output(
    platform: str,
    result: subprocess.CompletedProcess[bytes],
    meta_data: OtaMetaData,
) -> None:
    if result.returncode != 0:
        print(result.stderr)
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

            meta_data[zip_id] = OtaArtifact(
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
            )


def retrieve_current_meta() -> OtaMetaData:
    meta: OtaMetaData = {}
    for platform in PLATFORMS:
        print(platform)
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
            platform,
            subprocess.run(cmd, capture_output=True),
            meta,
        )

        beta_cmd = cmd.copy()
        beta_cmd.append("--beta")
        parse_download_meta_output(
            platform,
            subprocess.run(beta_cmd, capture_output=True),
            meta,
        )

    return meta

def merge_meta_data(ours: OtaMetaData, theirs: OtaMetaData) -> None:
    for their_zip_id, their_item in theirs.items():
        if their_zip_id in ours.keys():
            our_item = ours[their_zip_id]
            if (
                their_item.description != our_item.description
                and len(their_item.description) != 0
                and their_item.description[0] not in our_item.description
            ):
                ours[their_zip_id].description.extend(their_item.description)

            # this is a little bit the core of the whole thing:
            # - what does apple consider identity?
            # - what is sufficient for sentry?
            # - how to migrate if identities change?
            if not (
                their_item.build == our_item.build
                and their_item.version == our_item.version
                and their_item.platform == our_item.platform
                and their_item.url == our_item.url
                and their_item.devices == our_item.devices
                and their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
            ):
                raise RuntimeError(
                    f"Same matching keys with different value:\n\tlocal: {our_item}\n\tapple: {their_item}"
                )
        else:
            ours[their_zip_id] = their_item

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
    total_mib = total / (1024 * 1024)
    logger.debug(f"OTA Filesize: {floor(total_mib)} MiB")

    # TODO: how much prefix for identity?
    filepath = (
        download_dir / f"{ota_meta.platform}_{ota_meta.version}_{ota_meta.build}_{ota_meta.id}.zip"
    )
    with open(filepath, "wb") as f:
        actual = 0
        last_print = 0.0
        for chunk in res.iter_content(chunk_size=8192):
            f.write(chunk)
            actual = actual + len(chunk)

            actual_mib = actual / (1024 * 1024)
            if actual_mib - last_print > 100:
                logger.debug(f"{floor(actual_mib)} MiB")
                last_print = actual_mib

    print(f"{floor(actual_mib)} MiB")
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
        self.meta = retrieve_current_meta()
        self.storage.save_meta(self.meta)

    def download(self) -> None:
        logger.debug(f"Downloading OTA images to {self.storage.bucket.name}")

        self.update_meta()

        with tempfile.TemporaryDirectory() as download_dir:
            for k, v in self.meta.items():
                if v.download_path:
                    continue

                ota_file = download_ota(v, Path(download_dir))
                self.storage.save_ota(v, ota_file)
