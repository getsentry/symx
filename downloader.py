import argparse
import pathlib
import re
import subprocess
import sys
from argparse import Namespace
from enum import Enum
from typing import List, Generator

import common


class OtaDownloadLogSection(Enum):
    NONE = 1
    ERROR = 2
    URL = 3
    OTA_LIST = 4


# this might be more general than just OTAs but gotta start somewhere
def ota_download_co_run(command: List[str]) -> Generator[str, None, int]:
    popen = subprocess.Popen(command, stderr=subprocess.PIPE, universal_newlines=True)
    if popen.stderr is None:
        cmd_str = " ".join(command)
        raise RuntimeError(f"failed to initialize stderr for `{cmd_str}`")

    for stdout_line in iter(popen.stderr.readline, ""):
        yield stdout_line

    popen.stderr.close()
    return popen.wait()


def download_otas(output_path: pathlib.Path, platform: str) -> None:
    ipsw_ota_download_command = [
        "ipsw",
        "download",
        "ota",
        "--output",
        str(output_path),
        "-y",
        "--platform",
        platform,
        "--resume-all",
        "--verbose",
    ]
    ipsw_ota_beta_download_command = ipsw_ota_download_command.copy()
    ipsw_ota_beta_download_command.append("--beta")

    ota_artifacts = common.load_ota_images_meta(output_path)
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

        # gather OTA zip_name-file names
        if section != OtaDownloadLogSection.OTA_LIST and line.startswith(
            "   • OTA(s):"
        ):
            section = OtaDownloadLogSection.OTA_LIST
            continue

        if section == OtaDownloadLogSection.OTA_LIST:
            zip_match = re.search(
                "^\s{6}• ([a-f0-9]{40}\.zip) build=(\w*).*name=(\w*).*version=([0-9.]*)",
                line,
            )
            if zip_match:
                parsed_artifact = common.OtaArtifact(
                    zip_name=zip_match.group(1),
                    build=zip_match.group(2),
                    name=zip_match.group(3),
                    version=zip_match.group(4)[4:],
                    platform=platform,
                    url=None,
                    models=[],
                    devices=[],
                    download_path=None,
                )

                if parsed_artifact.zip_name in ota_artifacts.keys():
                    stored_artifact = ota_artifacts[parsed_artifact.zip_name]
                    if not (
                        stored_artifact.build == parsed_artifact.build
                        and stored_artifact.name == parsed_artifact.name
                        and stored_artifact.version == parsed_artifact.version
                        and stored_artifact.platform == parsed_artifact.platform
                        and stored_artifact.zip_name == parsed_artifact.zip_name
                    ):
                        raise RuntimeError(
                            f"Found OtaArtifact that doesn't match our meta-data\n"
                        )
                else:
                    ota_artifacts[parsed_artifact.zip_name] = parsed_artifact
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
                zip_name = url[url.rfind("/") + 1 :]
                if (
                    zip_name in ota_artifacts.keys()
                    and ota_artifacts[zip_name].url is not None
                    and ota_artifacts[zip_name].url != url
                ):
                    raise RuntimeError(
                        f"Duplicate OTA image zip with differing source URL detected: {zip_name}"
                        f"\n\told source URL: {ota_artifacts[zip_name].url}"
                        f"\n\tnew source URL: {url}"
                    )
                else:
                    ota_artifacts[zip_name].url = url
                continue
            else:
                section = OtaDownloadLogSection.NONE
                common.save_ota_images_meta(ota_artifacts, output_path)

    ota_download_co_run(ipsw_ota_beta_download_command)


def parse_args() -> Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output_path",
        dest="output_path",
        required=True,
        type=common.directory_arg_type,
        help="path to the output directory where the extracted symbols are placed",
    )
    return parser.parse_args()


def validate_shell_deps() -> None:
    version = common.ipsw_version()
    if version:
        print(f"Using ipsw {version}")
    else:
        print("ipsw not installed")
        sys.exit(1)


def main() -> None:
    args = parse_args()
    validate_shell_deps()
    for platform in common.OTA_PLATFORMS:
        download_otas(pathlib.Path(args.output_path), platform)


if __name__ == "__main__":
    main()
