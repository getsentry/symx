import argparse
import subprocess
import sys
from argparse import Namespace
from dataclasses import dataclass

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


@dataclass(frozen=True)
class ota_artifact:
    build: str
    device_count: int
    model_count: int
    name: str
    size: str
    zip: str
    url: str


# TODO: this might be more general than just OTAs but gotta start somewhere
def ota_download_co_run(command):
    popen = subprocess.Popen(command, stderr=subprocess.PIPE, universal_newlines=True)
    for stdout_line in iter(popen.stderr.readline, ""):
        yield stdout_line
    popen.stderr.close()
    return popen.wait()


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
        "--verbose",
    ]
    # TODO: also store the source (at least URL) of the download
    ipsw_ota_beta_download_command = ipsw_ota_download_command.copy()
    ipsw_ota_beta_download_command.append("--beta")

    error_log = False
    for line in ota_download_co_run(ipsw_ota_download_command):
        # ignore error logs
        if line.find("• [ERROR]") != -1:
            error_log = True
            continue

        if error_log and line.startswith("}"):
            error_log = False
            continue

        if error_log:
            continue

        print(line)

    ota_download_co_run(ipsw_ota_beta_download_command)


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
