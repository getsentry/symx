import os
from google.cloud import storage

BUCKET_NAME = os.environ["BUCKET_NAME"]
PROJECT_ID = os.environ.get("PROJECT", None)
metadata_path = 'metadata.json'

storage_client = storage.Client(project=PROJECT_ID)

# Get the GCP bucket object
bucket = storage_client.bucket(BUCKET_NAME)

blob = bucket.blob(metadata_path)
if blob.exists():
    print(f'The file {metadata_path} exists in the GCP bucket.')
else:
    print(f'The file {metadata_path} does not exist in the GCP bucket.')
