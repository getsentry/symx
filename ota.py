import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

from filelock import FileLock
from google.cloud.storage import Client as StorageClient  # type: ignore

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
    meta_data: dict[str, OtaArtifact],
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

            meta_data[zip_id] = OtaArtifact(
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


def retrieve_current_meta() -> dict[str, OtaArtifact]:
    meta = {}
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

        ota_beta_download_meta_cmd = cmd.copy()
        ota_beta_download_meta_cmd.append("--beta")
        parse_download_meta_output(
            platform,
            subprocess.run(ota_beta_download_meta_cmd, capture_output=True),
            meta,
        )

    return meta


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


def save_ota_images_meta(theirs: dict[str, OtaArtifact], save_dir: Path) -> None:
    save_path = save_dir / ARTIFACTS_META_JSON
    lock_path = save_path.parent / (save_path.name + ".lock")

    ours = {}
    with FileLock(lock_path, timeout=5):
        with open(save_path) as fp:
            for k, v in json.load(fp).items():
                ours[k] = OtaArtifact(**v)

        merge_meta_data(ours, theirs)

        with open(save_path, "w") as fp:
            json.dump(ours, fp, cls=common.DataClassJSONEncoder)


def merge_meta_data(ours, theirs):
    for their_zip_id, their_item in theirs.items():
        if their_zip_id in ours.keys():
            our_item = ours[their_zip_id]
            # TODO: this is at the core of the question what is enough identity for apple, what is enough identity
            #       for sentry. If in an artifact everything stays the same except the description, is it the same
            #       artifact? What should we do about it? Example: watchOS95DevBeta1 vs watchOS95PublicBeta1
            if not (
                their_item.build == our_item.build
                and their_item.description == our_item.description
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
            pass
        else:
            ours[their_zip_id] = their_item


PROJECT_ID = "glassy-totality-296020"
BUCKET_NAME = "apple_ota_store"


def load_meta_from_gcs() -> dict[str, OtaArtifact]:
    result = {}
    storage_client = StorageClient(project=PROJECT_ID)
    bucket = storage_client.get_bucket(BUCKET_NAME)
    blob = bucket.blob(ARTIFACTS_META_JSON)
    if not blob.exists():
        return result

    with tempfile.NamedTemporaryFile() as f:
        blob.download_to_filename(f.name)
        for k, v in json.load(f.file).items():
            result[k] = OtaArtifact(**v)

    return result


def save_meta_to_gcs(theirs: dict[str, OtaArtifact]) -> None:
    storage_client = StorageClient(project=PROJECT_ID)
    bucket = storage_client.get_bucket(BUCKET_NAME)
    blob = bucket.blob(ARTIFACTS_META_JSON)
    ours = {}
    if blob.exists():
        with tempfile.NamedTemporaryFile() as f:
            blob.download_to_filename(f.name)
            generation_match_precondition = blob.generation
            for k, v in json.load(f.file).items():
                ours[k] = OtaArtifact(**v)
    else:
        generation_match_precondition = 0

    merge_meta_data(ours, theirs)
    blob.upload_from_string(
        json.dumps(ours, cls=common.DataClassJSONEncoder),
        if_generation_match=generation_match_precondition,
    )
