import dataclasses
import json
import os
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Optional, List, Any

from filelock import FileLock

OTA_ARTIFACTS_META_JSON = "ota_image_meta.json"

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


def load_ota_images_meta(load_dir: pathlib.Path) -> dict[str, OtaArtifact]:
    load_path = load_dir / OTA_ARTIFACTS_META_JSON
    result = {}
    if load_path.is_file():
        with FileLock(load_path.with_suffix(".lock"), timeout=5):
            try:
                with open(load_path) as fp:
                    for k, v in json.load(fp).items():
                        result[k] = OtaArtifact(**v)
            except OSError:
                pass
    return result


def save_ota_images_meta(
    meta_data: dict[str, OtaArtifact], save_dir: pathlib.Path
) -> None:
    save_path = save_dir / OTA_ARTIFACTS_META_JSON

    with FileLock(save_path.with_suffix(".lock"), timeout=5):
        with open(save_path, "w") as fp:
            json.dump(meta_data, fp, cls=DataClassJSONEncoder)


def directory_arg_type(path: str) -> str:
    if os.path.isdir(path):
        return path

    raise ValueError(f"Error: {path} is not a valid directory")


def ipsw_version() -> str:
    result = subprocess.run(["ipsw", "version"], capture_output=True, check=True)
    output = result.stdout.decode("utf-8")
    match = re.search("Version: (.*),", output)
    if match:
        version = match.group(1)
        return version

    raise RuntimeError(f"Couldn't parse version from ipsw output: {output}")
