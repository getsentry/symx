import json
from typing import Optional

from google.cloud.storage import Bucket, Blob  # type: ignore

from symx._common import DataClassJSONEncoder
from symx._gcs import convert_image_name_to_path, download_and_hydrate_meta
from symx._ota import ARTIFACTS_META_JSON, OtaMetaData, OtaProcessingState


def _apply_new_directory_layout(bucket: Bucket) -> Optional[Blob]:
    blob: Blob
    meta_blob: Optional[Blob] = None
    for blob in bucket.list_blobs():
        image_name: str = blob.name
        if image_name.endswith(".zip"):
            # Step 1: rename all files to our new folder layout
            bucket.rename_blob(
                blob,
                convert_image_name_to_path(image_name),
            )
        if image_name == ARTIFACTS_META_JSON:
            meta_blob = blob

    return meta_blob


def _update_meta(meta: OtaMetaData) -> None:
    ota_id = "d3e35075eee610c4c54c0dd94a35b46c22ce9cbe"

    # Step 2: make sure the new filenames are reflected in the mirrored artifacts
    meta[ota_id].download_path = convert_image_name_to_path(
        "ios_16.5_20F66_d3e35075eee610c4c54c0dd94a35b46c22ce9cbe.zip"
    )

    # Step 3: make sure "mirrored" state is recorded
    meta[ota_id].processing_state = OtaProcessingState.MIRRORED

    # Step 4: set last run to where it was downloaded (since the error was unrelated to the artifact processed)
    meta[ota_id].last_run = 5122972649


def migrate(bucket: Bucket) -> None:
    meta_blob = _apply_new_directory_layout(bucket)
    if not meta_blob:
        raise RuntimeError(
            f"Failed to find {ARTIFACTS_META_JSON} in bucket {bucket.name}"
        )

    meta, generation = download_and_hydrate_meta(meta_blob)
    _update_meta(meta)
    meta_blob.upload_from_string(
        json.dumps(meta, cls=DataClassJSONEncoder),
        if_generation_match=generation,
    )
