import argparse
import re
import subprocess
import sys
from argparse import Namespace
from dataclasses import dataclass
from enum import Enum
from typing import Optional

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


@dataclass
class OtaArtifact:
    build: str
    name: str
    version: str
    platform: str
    zip: str
    url: Optional[str]
    download_path: Optional[str]
    devices: [str]
    models: [str]


class OtaDownloadLogSection(Enum):
    NONE = 1
    ERROR = 2
    URL = 3
    OTA_LIST = 4


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

    # TODO:
    #  - ota_images must be loaded from disk on startup
    #  - ota_images must be persisted before download starts
    #  - ota_images must be updated while downloads/start complete
    #  - the above requires concurrent reading of stderr/stdout
    ota_images = {}
    # replace these with an enum
    section = OtaDownloadLogSection.NONE
    for line in ota_download_co_run(ipsw_ota_download_command):
        # ignore error logs
        if section == OtaDownloadLogSection.ERROR:
            if line.startswith("}"):
                section = OtaDownloadLogSection.NONE
            continue
        else:
            if line.find("• [ERROR]") != -1:
                section = OtaDownloadLogSection.ERROR
                continue

        # ignore first fetch log
        if line.startswith("   • name: "):
            continue

        # gather OTA zip-file names
        if section != OtaDownloadLogSection.OTA_LIST and line.startswith(
            "   • OTA(s):"
        ):
            section = OtaDownloadLogSection.OTA_LIST
            continue

        # TODO: do we need this? do we need to extract more from this?
        if section == OtaDownloadLogSection.OTA_LIST:
            zip_match = re.search(
                "^\s{6}• ([a-f0-9]{40}\.zip) build=(\w*).*name=(\w*).*version=([0-9.]*)",
                line,
            )
            if zip_match:
                ota_zip_name = zip_match.group(1)
                ota_images[ota_zip_name] = OtaArtifact(
                    build=zip_match.group(2),
                    name=zip_match.group(3),
                    version=zip_match.group(4)[4:],
                    platform=platform,
                    zip=ota_zip_name,
                    url=None,
                    models=[],
                    devices=[],
                    download_path=None,
                )
                continue
            else:
                section = OtaDownloadLogSection.NONE

        # capture URL log
        if section != OtaDownloadLogSection.URL and line.startswith(
            "   • URLs to Download:"
        ):
            section = OtaDownloadLogSection.URL
            continue

        if section == OtaDownloadLogSection.URL:
            url_match = re.search("\s{6}• (http(.*)\.zip)", line)
            if url_match:
                url = url_match.group(1)
                ota_zip_name = url[url.rfind("/") + 1 :]
                if (
                    ota_zip_name in ota_images.keys()
                    and ota_images[ota_zip_name].url is not None
                ):
                    print(f"Unexpected duplicate OTA image zip: {ota_zip_name}: ")
                    print(f"\told source URL: {ota_images[ota_zip_name].url}")
                    print(f"\tnew source URL: {url}")
                    raise RuntimeError("Duplicate OTA image zip detected")
                else:
                    ota_images[ota_zip_name].url = url
                continue
            else:
                section = OtaDownloadLogSection.NONE

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


def validate_shell_deps() -> None:
    version = util.ipsw_version()
    if version:
        print(f"Using ipsw {version}")
    else:
        print("ipsw not installed")
        sys.exit(1)


def main() -> None:
    args = parse_args()
    validate_shell_deps()
    for platform in OTA_PLATFORMS:
        download_otas(args.output_path, platform)


if __name__ == "__main__":
    main()
