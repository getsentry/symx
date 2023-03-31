from google.cloud.storage import Client as StorageClient  # type: ignore

import ota

PROJECT_ID = "glassy-totality-296020"
BUCKET_NAME = "apple_ota_store"


def update_ota_metadata() -> None:
    print("Updating meta-data for...")

    new_meta_data = ota.retrieve_current_meta()
    ota.save_meta_to_gcs(new_meta_data)


def gcs_ota_downloader() -> None:
    storage_client = StorageClient(project=PROJECT_ID)
    bucket = storage_client.bucket(BUCKET_NAME)
    blobs = storage_client.list_blobs(BUCKET_NAME)
    for blob in blobs:
        print(blob.name)


if __name__ == "__main__":
    gcs_ota_downloader()
