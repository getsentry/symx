import argparse
import subprocess
import sys
from argparse import Namespace

import util

OTA_PLATFORMS = [
    "ios",
    "watchos",
    "tvos",
    "audioos",
    "accessory",
    "macos",
    "recovery",
]


def download_otas(output_path: str, platform: str):
    ipsw_ota_download_command = [
        "ipsw",
        "download",
        "ota",
        "--output",
        output_path,
        "-y",
        "--platform",
        platform,
        "--resume-all",
    ]
    ipsw_ota_beta_download_command = ipsw_ota_download_command.copy()
    ipsw_ota_beta_download_command.append("--beta")
    subprocess.run(ipsw_ota_download_command)
    subprocess.run(ipsw_ota_beta_download_command)


def parse_args() -> Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output_path",
        dest="output_path",
        required=True,
        type=util.directory,
        help="path to the output directory where the extracted symbols are placed",
    )
    return parser.parse_args()


def validate_shell_deps():
    version = util.ipsw_version()
    if version:
        print(f"Using ipsw {version}")
    else:
        print("ipsw not installed")
        sys.exit(1)


def main():
    args = parse_args()
    validate_shell_deps()
    for platform in OTA_PLATFORMS:
        download_otas(args.output_path, platform)


if __name__ == "__main__":
    main()
