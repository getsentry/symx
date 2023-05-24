import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import ota_meta_fs
from symx._common import Arch, directory_arg_type, ipsw_version

DYLD_SHARED_CACHE = "dyld_shared_cache"


@dataclass
class DSCSearchResult:
    arch: Arch
    artifact: Path
    split_dir: Optional[Path]


@dataclass(frozen=True)
class MountInfo:
    dev: str
    id: str
    point: Path


@dataclass(frozen=True)
class ArtifactCategory:
    platform: str
    version: str
    build: str


def parse_args() -> Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input_dir",
        dest="input_dir",
        required=True,
        type=directory_arg_type,
        help="path to the input directory that is scanned for images to extract symbols from",
    )
    parser.add_argument(
        "--output_dir",
        dest="output_dir",
        required=True,
        help="path to the output directory where the extracted symbols are placed",
    )
    return parser.parse_args()


def validate_shell_deps() -> None:
    version = ipsw_version()
    if version:
        print(f"Using ipsw {version}")
    else:
        print("ipsw not installed")
        sys.exit(1)

    result = subprocess.run(["./symsorter", "--version"], capture_output=True)
    if result.returncode == 0:
        symsorter_version = result.stdout.decode("utf-8")
        print(f"Using {symsorter_version}")
    else:
        # TODO: download symsorter if missing or outdated?
        print("Cannot find symsorter in CWD")
        sys.exit(1)


def scan_input_dir(input_dir: Path) -> list[Path]:
    result = []
    for artifact in glob.iglob(str(input_dir) + "/**/*.zip", recursive=True):
        result.append(Path(artifact))
    return result


def patch_cryptex_dmg(artifact: Path, output_dir: Path) -> dict[str, Path]:
    dmg_files = {}
    result = subprocess.run(
        ["ipsw", "ota", "patch", str(artifact), "--output", str(output_dir)],
        capture_output=True,
    )
    if result.returncode == 0 and result.stderr != b"":
        for line in result.stderr.decode("utf-8").splitlines():
            re_match = re.search("Patching (.*) to (.*)", line)
            if re_match:
                dmg_files[re_match.group(1)] = Path(re_match.group(2))

    return dmg_files


def find_system_os_dmgs(search_dir: Path) -> list[Path]:
    result = []
    for artifact in glob.iglob(str(search_dir) + "/**/SystemOS/*.dmg", recursive=True):
        result.append(Path(artifact))
    return result


def parse_hdiutil_mount_output(cmd_output: str) -> MountInfo:
    mount_info = cmd_output.splitlines().pop().split()
    return MountInfo(mount_info[0], mount_info[1], Path(mount_info[2]))


def mount_and_split_dsc(dmg: Path, output_dir: Path) -> list[DSCSearchResult]:
    result = subprocess.run(["hdiutil", "mount", dmg], capture_output=True, check=True)

    mount = parse_hdiutil_mount_output(result.stdout.decode("utf-8"))
    split_dsc_dir = split_dsc(
        mount.point, output_dir / (mount.point.name + "_libraries")
    )
    result = subprocess.run(["hdiutil", "detach", mount.dev], capture_output=True)
    print(f"\t\t\tResult from detach: {result}")

    return split_dsc_dir


def split_dsc(input_dir: Path, output_dir: Path) -> list[DSCSearchResult]:
    print(f"\t\tSplitting {DYLD_SHARED_CACHE} of {input_dir}")
    dsc_search_results = find_dsc(input_dir)
    for search_result in dsc_search_results:
        # TODO: if we don't create a separate tmp directory here, we are potentially overwriting shit
        search_result.split_dir = output_dir
        # TODO: this file fails kinda silently with two results (test in detail):
        #  /Users/mischan/devel/tmp/ota_downloads/iOS16.3.2_OTAs/AppleTV14,1_a7c3d4ce39aaeebd94e975e0520a0754deff506a.zip
        result = subprocess.run(
            [
                "ipsw",
                "dyld",
                "split",
                search_result.artifact,
                search_result.split_dir,
            ],
            capture_output=True,
        )
        print(f"\t\t\tResult from split: {result}")
    return dsc_search_results


def find_dsc(input_dir: Path) -> list[DSCSearchResult]:
    # TODO: are we also interested in the DriverKit dyld_shared_cache?
    #  System/DriverKit/System/Library/dyld/
    dsc_path_prefix_options = [
        "System/Library/Caches/com.apple.dyld/",
        "AssetData/payloadv2/patches/System/Library/Caches/com.apple.dyld/",
        "AssetData/payloadv2/ecc_data/System/Library/Caches/com.apple.dyld/",
    ]

    dsc_search_results = []
    for path_prefix in dsc_path_prefix_options:
        for arch in Arch:
            dsc_path = input_dir / (path_prefix + DYLD_SHARED_CACHE + "_" + arch.value)
            if os.path.isfile(dsc_path):
                dsc_search_results.append(
                    DSCSearchResult(arch=arch, artifact=dsc_path, split_dir=None)
                )

    if len(dsc_search_results) == 0:
        raise RuntimeError(
            f"Couldn't find any {DYLD_SHARED_CACHE} paths in {input_dir}"
        )
    elif len(dsc_search_results) > 1:
        printable_paths = "\n".join(
            [str(result.artifact) for result in dsc_search_results]
        )
        print(
            f"Found more than one {DYLD_SHARED_CACHE} path in {input_dir}:\n{printable_paths}"
        )

    return dsc_search_results


def symsort(dsc_split_dir: Path, output_dir: Path, prefix: str, bundle_id: str) -> None:
    print(f"\t\t\tSymsorting {dsc_split_dir} to {output_dir}")
    # TODO: symsorter just writes into the output_path, but in the final scenario we want to change this behavior
    #  to check whether there would be any overwrites, if those overwrites actually contain different content (vs.
    #  just different meta-data) and then atomically do (or not) the write to final output directory.

    subprocess.run(
        [
            "./symsorter",
            "-zz",
            "-o",
            output_dir,
            "--prefix",
            prefix,
            "--bundle-id",
            bundle_id,
            dsc_split_dir,
        ],
        check=True,
    )


def find_path_prefix_in_dsc_extract_cmd_output(
    cmd_output: str, top_output_dir: Path
) -> Path:
    for line in cmd_output.splitlines():
        top_output_path_index = line.find(str(top_output_dir))
        if top_output_path_index == -1:
            continue

        extraction_name_start = top_output_path_index + len(str(top_output_dir)) + 1
        extraction_name_end = line.find("/", extraction_name_start)
        if extraction_name_end == -1:
            continue

        return top_output_dir / line[extraction_name_start:extraction_name_end]

    raise RuntimeError(f"Couldn't find path_prefix in command-output: {cmd_output}")


class DscNoMetaData(Exception):
    pass


class DscExtractionFailed(Exception):
    pass


def extract_ota(artifact: Path, output_dir: Path) -> Path:
    result = subprocess.run(
        [
            "ipsw",
            "ota",
            "extract",
            artifact,
            DYLD_SHARED_CACHE,
            "-o",
            output_dir,
        ],
        capture_output=True,
    )
    if result.returncode == 1:
        error_lines = []
        for line in result.stderr.decode("utf-8").splitlines():
            if line.startswith("   ⨯"):
                error_lines.append(line)
        errors = "\n\t".join(error_lines)
        raise DscExtractionFailed(
            f"Failed to extract {DYLD_SHARED_CACHE} from {artifact}:\n\t{errors}"
        )
    else:
        print(f"\t\tSuccessfully extracted {DYLD_SHARED_CACHE} from: {artifact}")
        return find_path_prefix_in_dsc_extract_cmd_output(
            result.stderr.decode("utf-8"),
            Path(output_dir),
        )


def split_and_symsort_dsc(
    input_dir: Path, category: ArtifactCategory, output_dir: Path
) -> None:
    with tempfile.TemporaryDirectory(suffix="_symex") as split_temp_dir:
        split_results = split_dsc(
            input_dir,
            Path(split_temp_dir),
        )
        symsort_split_results(split_results, category, output_dir)


def extract_dyld_cache(artifact: Path, input_dir: Path, output_dir: Path) -> None:
    meta_data = ota_meta_fs.load_meta_from_fs(input_dir)
    zip_id = artifact.name[artifact.name.rfind("_") + 1 : -4]
    if zip_id not in meta_data.keys():
        raise DscNoMetaData(
            f"Couldn't find id {zip_id} in meta-data (from artifact: {artifact})"
        )

    category = ArtifactCategory(
        meta_data[zip_id].platform,
        meta_data[zip_id].build,
        meta_data[zip_id].version,
    )

    with tempfile.TemporaryDirectory(suffix="_symex") as cryptex_patch_dir:
        print(f"Trying patch_cryptex_dmg with {artifact}")
        extracted_dmgs = patch_cryptex_dmg(artifact, Path(cryptex_patch_dir))
        if len(extracted_dmgs) != 0:
            print(
                f"\tCryptex patch successful. Mount, split, symsorting {DYLD_SHARED_CACHE} for: {artifact}"
            )
            process_cryptex_dmg(extracted_dmgs, category, output_dir)
        else:
            print(f"\tNot a cryptex, so extracting OTA {DYLD_SHARED_CACHE} directly")
            with tempfile.TemporaryDirectory(suffix="_symex") as extract_dsc_tmp_dir:
                extracted_dsc_dir = extract_ota(artifact, Path(extract_dsc_tmp_dir))
                print(f"\t\tSplitting & symsorting {DYLD_SHARED_CACHE} for: {artifact}")
                split_and_symsort_dsc(extracted_dsc_dir, category, output_dir)


def process_cryptex_dmg(
    extracted_dmgs: dict[str, Path],
    category: ArtifactCategory,
    output_dir: Path,
) -> None:
    with tempfile.TemporaryDirectory(suffix="_symex") as mount_and_split_temp_dir:
        split_results = mount_and_split_dsc(
            extracted_dmgs["cryptex-system-arm64e"],
            Path(mount_and_split_temp_dir),
        )
        symsort_split_results(split_results, category, output_dir)


def symsort_split_results(
    split_cache_results: list[DSCSearchResult],
    category: ArtifactCategory,
    output_dir: Path,
) -> None:
    for split_result in split_cache_results:
        if not split_result.split_dir:
            continue

        symsort(
            split_result.split_dir,
            output_dir,
            category.platform,
            f"{category.version}_{category.build}_{split_result.arch.value}",
        )
        # TODO: after the symsorter ran, we could update the meta-data of each debug-id with a ref to the artifact


def list_dsc_files(artifact: Path) -> None:
    ps = subprocess.Popen(["ipsw", "ota", "ls", str(artifact)], stdout=subprocess.PIPE)
    try:
        output = subprocess.check_output(("grep", DYLD_SHARED_CACHE), stdin=ps.stdout)
        ps.wait()
        print(output.decode("utf-8"))
    except subprocess.CalledProcessError:
        print(f"no {DYLD_SHARED_CACHE} found in {str(artifact)}")


def log_artifact_as(name: str, artifact: Path) -> None:
    with open(name, "a") as process_log_file:
        process_log_file.write(str(artifact) + "\n")


def load_artifact_log(name: str) -> list[Path]:
    try:
        with open(name) as process_log_file:
            return [
                Path(artifact_str)
                for artifact_str in process_log_file.read().splitlines()
            ]
    except FileNotFoundError:
        return []


def gather_images_to_process(input_dir: Path) -> list[Path]:
    new_images = scan_input_dir(input_dir)
    old_images = load_artifact_log("processed")
    old_images.extend(load_artifact_log("no_meta_data"))
    old_images.extend(load_artifact_log("extraction_failed"))
    to_process = list(set(new_images) - set(old_images))
    return to_process


def main() -> None:
    args = parse_args()
    validate_shell_deps()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    to_process = gather_images_to_process(input_dir)
    if len(to_process) == 0:
        return

    print(f"Processing {to_process}")

    if not output_dir.is_dir():
        output_dir.mkdir()

    for artifact in to_process:
        try:
            extract_dyld_cache(artifact, input_dir, output_dir)
            log_artifact_as("processed", artifact)
        except DscNoMetaData as e:
            print(e)
            log_artifact_as("no_meta_data", artifact)
        except DscExtractionFailed as e:
            print(e)
            log_artifact_as("extraction_failed", artifact)


if __name__ == "__main__":
    main()
