import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

from filelock import FileLock
from google.cloud.exceptions import PreconditionFailed
from google.cloud.storage import Client as StorageClient, Blob  # type: ignore

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


def load_meta_from_fs(load_dir: Path) -> OtaMetaData:
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


def save_ota_images_meta(theirs: OtaMetaData, save_dir: Path) -> None:
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


PROJECT_ID = "glassy-totality-296020"
BUCKET_NAME = "apple_ota_store"


def download_meta_blob(blob: Blob) -> Tuple[OtaMetaData, int]:
    result: OtaMetaData = {}
    with tempfile.NamedTemporaryFile() as f:
        blob.download_to_filename(f.name)
        generation = blob.generation
        for k, v in json.load(f.file).items():
            result[k] = OtaArtifact(**v)

    return result, generation


def load_meta_from_gcs() -> OtaMetaData:
    result: OtaMetaData = {}
    storage_client = StorageClient(project=PROJECT_ID)
    bucket = storage_client.get_bucket(BUCKET_NAME)
    blob = bucket.blob(ARTIFACTS_META_JSON)
    if not blob.exists():
        return result

    result, _ = download_meta_blob(blob)

    return result


def save_meta_to_gcs(theirs: OtaMetaData) -> OtaMetaData:
    storage_client = StorageClient(project=PROJECT_ID)
    bucket = storage_client.get_bucket(BUCKET_NAME)
    retry = 5

    while retry > 0:
        blob = bucket.blob(ARTIFACTS_META_JSON)
        if blob.exists():
            ours, generation_match_precondition = download_meta_blob(blob)
        else:
            ours, generation_match_precondition = {}, 0

        merge_meta_data(ours, theirs)
        try:
            blob.upload_from_string(
                json.dumps(ours, cls=common.DataClassJSONEncoder),
                if_generation_match=generation_match_precondition,
            )
            return ours
        except PreconditionFailed:
            retry = retry - 1

    raise RuntimeError("Failed to update meta-data")
