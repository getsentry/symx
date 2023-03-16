import argparse
import dataclasses
import json
import pathlib
import re
import subprocess
import sys
from argparse import Namespace
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Generator, Any

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


class DataClassJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


@dataclass
class OtaArtifact:
    build: str
    name: str
    version: str
    platform: str
    zip_name: str
    url: Optional[str]
    download_path: Optional[str]
    devices: List[str]
    models: List[str]


class OtaDownloadLogSection(Enum):
    NONE = 1
    ERROR = 2
    URL = 3
    OTA_LIST = 4


# TODO: this might be more general than just OTAs but gotta start somewhere
def ota_download_co_run(command: List[str]) -> Generator[str, None, int]:
    popen = subprocess.Popen(command, stderr=subprocess.PIPE, universal_newlines=True)
    if popen.stderr is None:
        cmd_str = " ".join(command)
        raise RuntimeError(f"failed to initialize stderr for `{cmd_str}`")

    for stdout_line in iter(popen.stderr.readline, ""):
        yield stdout_line

    popen.stderr.close()
    return popen.wait()


def load_ota_images_meta(load_dir: pathlib.Path) -> dict[str, OtaArtifact]:
    load_path = load_dir / "ota_image_meta.json"
    result = {}
    if load_path.is_file():
        try:
            with open(load_path) as fp:
                deser = json.load(fp)
                for k, v in deser.items():
                    result[k] = OtaArtifact(**v)
        except OSError:
            pass
    return result


def save_ota_images_meta(
    meta_data: dict[str, OtaArtifact], save_dir: pathlib.Path
) -> None:
    save_path = save_dir / "ota_image_meta.json"

    with open(save_path, "w") as fp:
        json.dump(meta_data, fp, cls=DataClassJSONEncoder)


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
    # TODO: also store the source (at least URL) of the download
    ipsw_ota_beta_download_command = ipsw_ota_download_command.copy()
    ipsw_ota_beta_download_command.append("--beta")

    # TODO:
    #  - ota_images must be loaded from disk on startup
    #  - ota_images must be persisted before download starts
    #  - ota_images must be updated while downloads/start complete
    #  - the above requires concurrent reading of stderr/stdout
    ota_images = load_ota_images_meta(output_path)
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

        # TODO: do we need this? do we need to extract more from this?
        if section == OtaDownloadLogSection.OTA_LIST:
            zip_match = re.search(
                "^\s{6}• ([a-f0-9]{40}\.zip_name) build=(\w*).*name=(\w*).*version=([0-9.]*)",
                line,
            )
            if zip_match:
                parsed_artifact = OtaArtifact(
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

                if parsed_artifact.zip_name in ota_images.keys():
                    stored_artifact = ota_images[parsed_artifact.zip_name]
                    print(f"stored = {stored_artifact}")
                    print(f"parsed = {parsed_artifact}")
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
                    ota_images[parsed_artifact.zip_name] = parsed_artifact
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
            url_match = re.search("\s{6}• (http(.*)\.zip_name)", line)
            if url_match:
                url = url_match.group(1)
                zip_name = url[url.rfind("/") + 1 :]
                if (
                    zip_name in ota_images.keys()
                    and ota_images[zip_name].url is not None
                ):
                    print(f"Unexpected duplicate OTA image zip_name: {zip_name}: ")
                    print(f"\told source URL: {ota_images[zip_name].url}")
                    print(f"\tnew source URL: {url}")
                    raise RuntimeError("Duplicate OTA image zip_name detected")
                else:
                    ota_images[zip_name].url = url
                continue
            else:
                section = OtaDownloadLogSection.NONE
                save_ota_images_meta(ota_images, output_path)

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
        download_otas(pathlib.Path(args.output_path), platform)


if __name__ == "__main__":
    main()
