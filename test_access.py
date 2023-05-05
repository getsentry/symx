import os
from google.cloud import storage
from google.auth import default

# Set the GCP bucket name
# bucket_name = os.environ['GCP_BUCKET_NAME']
# metadata_path = os.environ['METADATA']
bucket_name = 'symbol-collector-dev'
metadata_path = 'metadata.json'

#credentials = default()[0]
storage_client = storage.Client(project=None)

# Get the GCP bucket object
bucket = storage_client.bucket(bucket_name)

blob = bucket.blob(metadata_path)
if blob.exists():
    print(f'The file {metadata_path} exists in the GCP bucket.')
else:
    print(f'The file {metadata_path} does not exist in the GCP bucket.')
