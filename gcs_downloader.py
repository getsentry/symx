import ota


def update_ota_metadata() -> None:
    print("Updating meta-data for...")

    new_meta_data = ota.retrieve_current_meta()
    ota.save_meta_to_gcs(new_meta_data)


def gcs_ota_downloader() -> None:
    update_ota_metadata()


if __name__ == "__main__":
    ota.load_meta_from_gcs()
    gcs_ota_downloader()
