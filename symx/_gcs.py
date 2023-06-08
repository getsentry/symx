import base64
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from google.cloud.exceptions import PreconditionFailed, NotFound
from google.cloud.storage import Blob, Client, Bucket  # type: ignore

from ._common import DataClassJSONEncoder
from ._ota import (
    OtaArtifact,
    OtaMetaData,
    merge_meta_data,
    ARTIFACTS_META_JSON,
    OtaProcessingState,
    OtaStorage,
    check_hash,
)

logger = logging.getLogger(__name__)


def convert_image_name_to_path(old_name: str) -> str:
    [platform, version, build, file] = old_name.split("_")
    return f"mirror/ota/{platform}/{version}/{build}/{file}"


def download_and_hydrate_meta(blob: Blob) -> Tuple[OtaMetaData, int]:
    result: OtaMetaData = {}
    with tempfile.NamedTemporaryFile() as f:
        blob.download_to_filename(f.name)
        generation = blob.generation
        for k, v in json.load(f.file).items():
            result[k] = OtaArtifact(**v)

    return result, generation


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
        block = f.read(2**16)
        while len(block) != 0:
            hash_md5.update(block)
            block = f.read(2**16)

    return base64.b64encode(hash_md5.digest()).decode()


class GoogleStorage(OtaStorage):
    def __init__(self, project: Optional[str], bucket: str) -> None:
        self.project = project
        self.client: Client = Client(project=self.project)
        self.bucket: Bucket = self.client.bucket(bucket)

    def name(self) -> str:
        return str(self.bucket.name)

    def save_meta(self, theirs: OtaMetaData) -> OtaMetaData:
        retry = 5

        while retry > 0:
            blob = self.bucket.blob(ARTIFACTS_META_JSON)
            if blob.exists():
                ours, generation_match_precondition = download_and_hydrate_meta(blob)
            else:
                ours, generation_match_precondition = {}, 0

            merge_meta_data(ours, theirs)
            try:
                blob.upload_from_string(
                    json.dumps(ours, cls=DataClassJSONEncoder),
                    if_generation_match=generation_match_precondition,
                )
                return ours
            except PreconditionFailed:
                retry = retry - 1

        raise RuntimeError("Failed to update meta-data")

    def load_meta(self) -> Optional[OtaMetaData]:
        blob = self.bucket.blob(ARTIFACTS_META_JSON)
        if blob.exists():
            ours, _ = download_and_hydrate_meta(blob)
        else:
            logger.warning("Failed to load meta-data")
            return None

        return ours

    def save_ota(
        self, ota_meta_key: str, ota_meta: OtaArtifact, ota_file: Path
    ) -> None:
        if not ota_file.is_file():
            raise RuntimeError("Path to upload must be a file")

        logger.info(f"Start uploading {ota_file.name} to {self.bucket.name}")
        mirror_filename = convert_image_name_to_path(ota_file.name)
        blob = self.bucket.blob(mirror_filename)
        if blob.exists():
            # if the existing remote file has the same MD5 hash as the file we are about to upload, we can go on without
            # uploading and only update meta, since that means some meta is still set to INDEXED instead of MIRRORED.
            # On the other hand, if the hashes differ, then we have a problem and should be getting out
            blob.reload()
            remote_hash = blob.md5_hash
            local_hash = _fs_md5_hash(ota_file)
            if remote_hash == local_hash:
                logger.info(
                    f'"{mirror_filename}" was already uploaded with matching MD5 hash. '
                    f'Updating meta for "{ota_meta_key}" accordingly.'
                )
            else:
                logger.error(
                    f'"{mirror_filename}" was already uploaded and MD5 hash differs from the one uploaded '
                    f"(remote = {remote_hash}, local = {local_hash}). "
                    f"Maybe we have insufficient identity or corrupted meta-data?"
                )
                return
        else:
            # this file will be split into considerable chunks: set timeout to something high
            blob.upload_from_filename(str(ota_file), timeout=3600)
            logger.info("Upload finished. Updating OTA meta-data.")

        ota_meta.download_path = mirror_filename
        ota_meta.processing_state = OtaProcessingState.MIRRORED
        ota_meta.update_last_run()
        self.update_meta_item(ota_meta_key, ota_meta)

    def load_ota(self, ota: OtaArtifact, download_dir: Path) -> Optional[Path]:
        blob = self.bucket.blob(ota.download_path)
        local_ota_path = download_dir / f"{ota.id}.zip"
        try:
            # TODO: figure out why this was necessary
            blob.reload()
        except NotFound:
            logger.error(
                f"The OTA references a mirror-path that is no longer accessible (probably TTL rn)"
            )
            return None

        blob.download_to_filename(str(local_ota_path))
        if not check_hash(ota, local_ota_path):
            logger.error(f"The SHA1 mismatch between storage and meta-data for OTA")
            return None

        return local_ota_path

    def update_meta_item(self, ota_meta_key: str, ota_meta: OtaArtifact) -> OtaMetaData:
        retry = 5

        while retry > 0:
            blob = self.bucket.blob(ARTIFACTS_META_JSON)
            if blob.exists():
                ours, generation_match_precondition = download_and_hydrate_meta(blob)
            else:
                ours, generation_match_precondition = {}, 0

            ours[ota_meta_key] = ota_meta
            try:
                blob.upload_from_string(
                    json.dumps(ours, cls=DataClassJSONEncoder),
                    if_generation_match=generation_match_precondition,
                )
                return ours
            except PreconditionFailed:
                retry = retry - 1

        raise RuntimeError("Failed to update meta-data item")

    def upload_symbols(
        self, input_dir: Path, ota_key: str, ota_meta: OtaArtifact, bundle_id: str
    ) -> None:
        dest_blob_prefix = Path("symbols")
        bundle_index_path = dest_blob_prefix / ota_meta.platform / "bundles" / bundle_id
        blob = self.bucket.blob(str(bundle_index_path))
        if blob.exists():
            logger.error(
                f"We already have a `bundle_id` {bundle_id} for {ota_meta.platform} in the symbol store. "
                f"Overwriting corrupts the `bundle_id` index. We must implement a merge strategy."
            )
            ota_meta.processing_state = OtaProcessingState.BUNDLE_DUPLICATION_DETECTED
            ota_meta.update_last_run()
            return

        for root, dirs, files in os.walk(input_dir):
            for file in files:
                local_file = Path(root) / file
                dest_blob_name = (
                    dest_blob_prefix / Path(root).relative_to(input_dir) / file
                )
                blob = self.bucket.blob(str(dest_blob_name))
                blob.upload_from_filename(str(local_file))
                logger.debug(f"File {local_file} uploaded to {dest_blob_name}.")

        ota_meta.processing_state = OtaProcessingState.SYMBOLS_EXTRACTED
        ota_meta.update_last_run()
