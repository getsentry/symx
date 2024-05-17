import json
import logging
import tempfile
from pathlib import Path

from google.cloud.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Client, Bucket  # type: ignore[import-untyped]

from symx._common import (
    DataClassJSONEncoder,
    ArtifactProcessingState,
    compare_md5_hash,
    parse_gcs_url,
    upload_symbol_binaries,
    try_download_to_filename,
)
from symx._ota import (
    OtaArtifact,
    OtaMetaData,
    merge_meta_data,
    ARTIFACTS_META_JSON,
    OtaStorage,
    check_ota_hash,
)

logger = logging.getLogger(__name__)


def convert_image_name_to_path(old_name: str) -> str:
    [platform, version, build, file] = old_name.split("_")
    return f"mirror/ota/{platform}/{version}/{build}/{file}"


def download_and_hydrate_meta(blob: Blob) -> tuple[OtaMetaData, int]:
    result: OtaMetaData = {}
    with tempfile.NamedTemporaryFile() as f:
        blob.download_to_filename(f.name)
        generation = blob.generation
        for k, v in json.load(f.file).items():
            result[k] = OtaArtifact(**v)

    if generation is None:
        generation = 0
    return result, generation


class OtaGcsStorage(OtaStorage):
    def __init__(self, project: str | None, bucket: str) -> None:
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

    def load_meta(self) -> OtaMetaData | None:
        blob = self.bucket.blob(ARTIFACTS_META_JSON)
        if blob.exists():
            ours, _ = download_and_hydrate_meta(blob)
        else:
            logger.warning("Failed to load meta-data")
            return None

        return ours

    def save_ota(self, ota_meta_key: str, ota_meta: OtaArtifact, ota_file: Path) -> None:
        if not ota_file.is_file():
            raise RuntimeError("Path to upload must be a file")

        logger.info(f"Start uploading {ota_file.name} to {self.bucket.name}")
        mirror_filename = convert_image_name_to_path(ota_file.name)
        blob = self.bucket.blob(mirror_filename)
        if blob.exists():
            # if the existing remote file has the same MD5 hash as the file we are about to upload, we can go on without
            # uploading and only update meta, since that means some meta is still set to INDEXED instead of MIRRORED.
            # On the other hand, if the hashes differ, then we have a problem and should be getting out
            if not compare_md5_hash(ota_file, blob):
                return
        else:
            # this file will be split into considerable chunks: set timeout to something high
            blob.upload_from_filename(str(ota_file), timeout=3600)
            logger.info("Upload finished. Updating OTA meta-data.")

        ota_meta.download_path = mirror_filename
        ota_meta.processing_state = ArtifactProcessingState.MIRRORED
        ota_meta.update_last_run()
        self.update_meta_item(ota_meta_key, ota_meta)

    def load_ota(self, ota: OtaArtifact, download_dir: Path) -> Path | None:
        blob = self.bucket.blob(ota.download_path)
        local_ota_path = download_dir / f"{ota.id}.zip"
        if not blob.exists():
            logger.error("The OTA references a mirror-path that is no longer accessible")
            return None

        if not try_download_to_filename(blob, local_ota_path):
            return None

        if not check_ota_hash(ota, local_ota_path):
            logger.error("The SHA1 mismatch between storage and meta-data for OTA")
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

    def upload_symbols(self, input_dir: Path, ota_meta_key: str, ota_meta: OtaArtifact, bundle_id: str) -> None:
        upload_symbol_binaries(self.bucket, ota_meta.platform, bundle_id, input_dir)
        ota_meta.processing_state = ArtifactProcessingState.SYMBOLS_EXTRACTED
        ota_meta.update_last_run()
        self.update_meta_item(ota_meta_key, ota_meta)


def init_storage(storage: str) -> OtaGcsStorage | None:
    uri = parse_gcs_url(storage)
    if uri is None or uri.hostname is None:
        return None
    return OtaGcsStorage(project=uri.username, bucket=uri.hostname)
