import json
import tempfile
import logging

from pathlib import Path
from typing import Optional, Tuple

from google.cloud.storage import Blob, Client  # type: ignore
from google.cloud.exceptions import PreconditionFailed

from ._common import DataClassJSONEncoder
from ._ota import OtaArtifact, OtaMetaData, merge_meta_data, ARTIFACTS_META_JSON

logger = logging.getLogger(__name__)


def _download_and_hydrate_meta(blob: Blob) -> Tuple[OtaMetaData, int]:
    result: OtaMetaData = {}
    with tempfile.NamedTemporaryFile() as f:
        blob.download_to_filename(f.name)
        generation = blob.generation
        for k, v in json.load(f.file).items():
            result[k] = OtaArtifact(**v)

    return result, generation


class GoogleStorage:
    def __init__(self, project: Optional[str], bucket: str) -> None:
        self.project = project
        self.client = Client(project=self.project)
        self.bucket = self.client.bucket(bucket)

    def save_meta(self, theirs: OtaMetaData) -> OtaMetaData:
        retry = 5

        while retry > 0:
            blob = self.bucket.blob(ARTIFACTS_META_JSON)
            if blob.exists():
                ours, generation_match_precondition = _download_and_hydrate_meta(blob)
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

    def save_ota(self, ota_meta: OtaArtifact, ota_file: Path) -> None:
        if not ota_file.is_file():
            raise RuntimeError("Path to upload must be a file")

        logger.info(f"Start uploading {ota_file.name} to {self.bucket.name}")
        blob = self.bucket.blob(ota_file.name)
        if blob.exists():
            raise RuntimeError(
                "This file was already uploaded, maybe we have an identity problem or corrupted"
                " meta-data"
            )

        # this file will be split into considerable chunks set timeout to something high
        blob.upload_from_filename(ota_file, timeout=3600)

        logger.info("Upload finished. Updating OTA meta-data.")
        ota_meta.download_path = ota_file.name
        self.update_meta_item(ota_meta)

    def update_meta_item(self, ota_meta: OtaArtifact) -> OtaMetaData:
        retry = 5

        while retry > 0:
            blob = self.bucket.blob(ARTIFACTS_META_JSON)
            if blob.exists():
                ours, generation_match_precondition = _download_and_hydrate_meta(blob)
            else:
                ours, generation_match_precondition = {}, 0

            ours[ota_meta.id] = ota_meta
            try:
                blob.upload_from_string(
                    json.dumps(ours, cls=DataClassJSONEncoder),
                    if_generation_match=generation_match_precondition,
                )
                return ours
            except PreconditionFailed:
                retry = retry - 1

        raise RuntimeError("Failed to update meta-data item")
