import subprocess
from pathlib import Path

import common
import ota


def download_otas(output_dir: Path, platform: str) -> None:
    ota_download_cmd = [
        "ipsw",
        "download",
        "ota",
        "--output",
        str(output_dir),
        "-y",
        "--platform",
        platform,
        "--resume-all",
    ]
    subprocess.run(ota_download_cmd)

    ota_beta_download_cmd = ota_download_cmd.copy()
    ota_beta_download_cmd.append("--beta")
    subprocess.run(ota_beta_download_cmd)


def download_ota_metadata(output_dir: Path) -> None:
    print("Updating meta-data for...")

    new_meta_data = ota.retrieve_current_meta()
    ota.save_ota_images_meta(new_meta_data, output_dir)


def main() -> None:
    args = common.downloader_parse_args()
    common.downloader_validate_shell_deps()

    # get the meta-data for all platforms first, so we can be sure to continuously update the meta-data store
    # for __all__ platforms everytime we start the downloader.
    download_ota_metadata(args.output_dir)

    # only now start with the mirroring process
    for platform in ota.PLATFORMS:
        print(f"Downloading OTAs for {platform}...")
        download_otas(args.output_dir, platform)


if __name__ == "__main__":
    main()
