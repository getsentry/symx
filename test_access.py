import os
import sentry_sdk
from google.cloud import storage

BUCKET_NAME = os.environ["BUCKET_NAME"]
PROJECT_ID = os.environ.get("PROJECT", None)
ARTIFACTS_META_JSON = "metadata.json"


def download_meta_from_gcs(bucket) -> None:
    # Get the GCP bucket object
    blob = bucket.blob(ARTIFACTS_META_JSON)

    # Download the file to a destination
    if blob.exists():
        blob.download_to_filename(ARTIFACTS_META_JSON)
        print(f"Blob {ARTIFACTS_META_JSON} downloaded to {ARTIFACTS_META_JSON}.")


if __name__ == "__main__":
    SENTRY_DSN = os.environ.get("SENTRY_DSN", None)
    if SENTRY_DSN:
        sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0)

    storage_client = storage.Client(project=PROJECT_ID)

    # Get the GCP bucket object
    bucket = storage_client.bucket(BUCKET_NAME)
    download_meta_from_gcs(bucket)

    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(BUCKET_NAME)
    download_meta_from_gcs(bucket)
