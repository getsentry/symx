import os
import sentry_sdk
from google.cloud import storage

BUCKET_NAME = os.environ["BUCKET_NAME"]
PROJECT_ID = os.environ.get("PROJECT", None)
ARTIFACTS_META_JSON = 'metadata.json'

storage_client = storage.Client(project=PROJECT_ID)

# Get the GCP bucket object
bucket = storage_client.bucket(BUCKET_NAME)

def load_meta_from_gcs(storage_client: storage.Client) -> {}:
    print(f"Loading meta-data from {BUCKET_NAME}/{ARTIFACTS_META_JSON}")
    result = {}
    bucket = storage_client.get_bucket(BUCKET_NAME)
    blob = bucket.blob(ARTIFACTS_META_JSON)
    if not blob.exists():
        return result

    return result

if __name__ == "__main__":
    SENTRY_DSN = os.environ.get("SENTRY_DSN", None)
    if SENTRY_DSN:
        sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0)
    load_meta_from_gcs(storage_client)
