from google.cloud.storage import Bucket, Blob  # type: ignore

from symx._gcs import GoogleStorage
from symx._ota import OtaProcessingState


def migrate(storage: GoogleStorage) -> None:
    # load all meta-data
    ota_meta = storage.load_meta()

    if ota_meta:
        for k, v in ota_meta.items():
            # reset each meta-data item marked as a `DUPLICATE` back to `MIRRORED`
            # to allow the extraction workflow to pick it up again
            if v.processing_state == OtaProcessingState.BUNDLE_DUPLICATION_DETECTED:
                v.processing_state = OtaProcessingState.MIRRORED
                v.update_last_run()
                storage.update_meta_item(k, v)
