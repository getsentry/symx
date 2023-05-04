import os
from google.cloud import storage

# Set the GCP bucket name
# bucket_name = os.environ['GCP_BUCKET_NAME']
# metadata_path = os.environ['METADATA']
bucket_name = 'symbol-collector-dev'
metadata_path = 'metadata.json'

# Authenticate with the GCP account using the GitHub Action token
storage_client = storage.Client(credentials=os.environ['GITHUB_TOKEN'])

# Get the GCP bucket object
bucket = storage_client.bucket(bucket_name)

blob = bucket.blob(metadata_path)
if blob.exists():
    print(f'The file {metadata_path} exists in the GCP bucket.')
else:
    print(f'The file {metadata_path} does not exist in the GCP bucket.')
