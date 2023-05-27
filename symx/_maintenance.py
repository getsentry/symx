import json
from typing import Optional

from google.cloud.storage import Bucket, Blob

from symx._common import DataClassJSONEncoder
from symx._gcs import convert_image_name_to_path, download_and_hydrate_meta
from symx._ota import ARTIFACTS_META_JSON, OtaMetaData, OtaArtifact, OtaProcessingState


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
    key: str
    ota: OtaArtifact
    beta_keys = []
    for key, ota in meta.items():
        # Step 2: let all metas reference our first long mirroring run
        meta[key].last_run = 5017535638
        if ota.download_path:
            # Step 3: make sure the new filenames are reflected in the mirrored artifacts
            meta[key].download_path = convert_image_name_to_path(ota.download_path)
            # Step 4: make sure "mirrored" state is recorded
            meta[key].processing_state = OtaProcessingState.MIRRORED

        # Step 5a: retrieve known BETAs...
        if ota.build in ("20F6066", "20L6563", "20L6562"):
            beta_keys.append(key)

    # Step 5b: ...and mark them as beta in the look-up key, so we
    # can keep them together with later releases of the same name
    for key in beta_keys:
        meta[key + "_beta"] = meta.pop(key)


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
