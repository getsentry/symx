import json
from pathlib import Path

from symx._common import DataClassJSONEncoder
from symx._ota import OtaMetaData, OtaArtifact, ARTIFACTS_META_JSON, merge_meta_data
from filelock import FileLock


def load_meta_from_fs(load_dir: Path) -> OtaMetaData:
    load_path = load_dir / ARTIFACTS_META_JSON
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


def save_ota_images_meta(theirs: OtaMetaData, save_dir: Path) -> None:
    save_path = save_dir / ARTIFACTS_META_JSON
    lock_path = save_path.parent / (save_path.name + ".lock")

    ours = {}
    with FileLock(lock_path, timeout=5):
        with open(save_path) as fp:
            for k, v in json.load(fp).items():
                ours[k] = OtaArtifact(**v)

        merge_meta_data(ours, theirs)

        with open(save_path, "w") as fp:
            json.dump(ours, fp, cls=DataClassJSONEncoder)
