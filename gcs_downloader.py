import os
import tempfile

from pathlib import Path

import ota
import sentry_sdk


def update_ota_metadata() -> ota.OtaMetaData:
    print("Updating meta-data for...")

    new_meta_data = ota.retrieve_current_meta()
    return ota.save_meta_to_gcs(new_meta_data)


def download_otas(meta_data: ota.OtaMetaData) -> None:
    with tempfile.TemporaryDirectory() as download_dir:
        for k, v in meta_data.items():
            if v.download_path:
                continue

            ota_file = ota.download_ota(v, Path(download_dir))
            ota.upload_ota_to_gcs(v, ota_file)
            # TODO: delete local file?


def gcs_ota_downloader() -> None:
    meta_data = update_ota_metadata()
    download_otas(meta_data)


if __name__ == "__main__":
    SENTRY_DSN = os.environ.get("SENTRY_DSN", None)
    if SENTRY_DSN:
        sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0)
    ota.load_meta_from_gcs()
    gcs_ota_downloader()
