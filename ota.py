import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

from filelock import FileLock

import common

ARTIFACTS_META_JSON = "ota_image_meta.json"

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
    description: Optional[str]
    version: str
    platform: str
    id: str
    url: str
    download_path: Optional[str]
    devices: Optional[List[str]]
    hash: str
    hash_algorithm: str


def parse_download_meta_output(
    platform: str,
    result: subprocess.CompletedProcess[bytes],
    meta_data_store: dict[str, OtaArtifact],
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

            if zip_id in meta_data_store.keys():
                store_item = meta_data_store[zip_id]
                if not (
                    store_item.build == meta_item["build"]
                    and store_item.description == meta_item.get("description")
                    and store_item.version == meta_item["version"]
                    and store_item.platform == platform
                    and store_item.url == url
                    and store_item.devices == meta_item.get("devices")
                    and store_item.hash == meta_item["hash"]
                    and store_item.hash_algorithm == meta_item["hash_algorithm"]
                ):
                    raise RuntimeError(
                        f"Same matching keys with different value:\n\tlocal: {store_item}\n\tapple: {meta_item}"
                    )
                pass
            else:
                meta_data_store[zip_id] = OtaArtifact(
                    id=zip_id,
                    build=meta_item["build"],
                    description=meta_item.get("description"),
                    version=meta_item["version"],
                    platform=platform,
                    url=url,
                    devices=meta_item.get("devices"),
                    download_path=None,
                    hash=meta_item["hash"],
                    hash_algorithm=meta_item["hash_algorithm"],
                )


def retrieve_current_meta(meta_data: dict[str, OtaArtifact]) -> None:
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
            meta_data,
        )

        ota_beta_download_meta_cmd = cmd.copy()
        ota_beta_download_meta_cmd.append("--beta")
        parse_download_meta_output(
            platform,
            subprocess.run(ota_beta_download_meta_cmd, capture_output=True),
            meta_data,
        )


def load_meta_from_fs(load_dir: Path) -> dict[str, OtaArtifact]:
    load_path = load_dir / ARTIFACTS_META_JSON
    lock_path = load_path.parent / (load_path.name + ".lock")
    result = {}
    if load_path.is_file():
        with FileLock(lock_path, timeout=5):
            try:
                with open(load_path) as fp:
                    for k, v in json.load(fp).items():
                        result[k] = OtaArtifact(**v)
            except OSError:
                pass
    return result


def save_ota_images_meta(meta_data: dict[str, OtaArtifact], save_dir: Path) -> None:
    save_path = save_dir / ARTIFACTS_META_JSON
    lock_path = save_path.parent / (save_path.name + ".lock")

    with FileLock(lock_path, timeout=5):
        with open(save_path, "w") as fp:
            json.dump(meta_data, fp, cls=common.DataClassJSONEncoder)


def load_meta_from_gcs() -> dict[str, OtaArtifact]:
    return {}


def save_meta_to_gcs(meta_data: dict[str, OtaArtifact]) -> None:
    return None
