import dataclasses
import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional, List, Any
from pathlib import Path

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


def load_ota_images_meta(load_dir: Path) -> dict[str, OtaArtifact]:
    load_path = load_dir / OTA_ARTIFACTS_META_JSON
    lock_path = load_path.parent / (load_path.name + ".lock")
    result = {}
    if load_path.is_file():
        with FileLock(lock_path, timeout=5):
            try:
                with open(load_path) as fp:
                    for k, v in json.load(fp).items():
                        result[k] = OtaArtifact(**v)
            except OSError:
                pass
    return result


def save_ota_images_meta(meta_data: dict[str, OtaArtifact], save_dir: Path) -> None:
    save_path = save_dir / OTA_ARTIFACTS_META_JSON
    lock_path = save_path.parent / (save_path.name + ".lock")

    with FileLock(lock_path, timeout=5):
        with open(save_path, "w") as fp:
            json.dump(meta_data, fp, cls=DataClassJSONEncoder)


def directory_arg_type(path: str) -> Path:
    if os.path.isdir(path):
        return Path(path)

    raise ValueError(f"Error: {path} is not a valid directory")


def ipsw_version() -> str:
    result = subprocess.run(["ipsw", "version"], capture_output=True, check=True)
    output = result.stdout.decode("utf-8")
    match = re.search("Version: (.*),", output)
    if match:
        version = match.group(1)
        return version

    raise RuntimeError(f"Couldn't parse version from ipsw output: {output}")
