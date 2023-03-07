import argparse
import glob
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

import util


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input_path",
        dest="input_path",
        required=True,
        type=util.directory,
        help="path to the input directory that is scanned for images to extract symbols from",
    )
    parser.add_argument(
        "--output_path",
        dest="output_path",
        required=True,
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

    result = subprocess.run(["./symsorter", "--version"], capture_output=True)
    if result.returncode == 0:
        symsorter_version = result.stdout.decode("utf-8")
        print(f"Using {symsorter_version}")
    else:
        # TODO: download symsorter if missing or outdated?
        print("Cannot find symsorter in CWD")
        sys.exit(1)


def scan_input_path(input_path: str) -> list[str]:
    result = []
    for file_path in glob.iglob(input_path + "/**/*.zip", recursive=True):
        result.append(file_path)
    return result


def patch_cryptex_dmg(item: str, output_path: str) -> dict[str, str]:
    dmg_files = {}
    result = subprocess.run(
        ["ipsw", "ota", "patch", item, "--output", output_path], capture_output=True
    )
    if result.returncode == 0 and result.stderr != b"":
        for line in result.stderr.decode("utf-8").splitlines():
            re_match = re.search("Patching (.*) to (.*)", line)
            if re_match:
                dmg_files[re_match.group(1)] = re_match.group(2)

    return dmg_files


def find_system_os_dmgs(path: str) -> list[str]:
    result = []
    for file_path in glob.iglob(path + "/**/SystemOS/*.dmg", recursive=True):
        result.append(file_path)
    return result


@dataclass(frozen=True)
class MountInfo:
    dev: str
    id: str
    point: str


def parse_hdiutil_mount_output(output: str) -> MountInfo:
    mount_info = output.splitlines().pop().split()
    return MountInfo(mount_info[0], mount_info[1], mount_info[2])


def mount_and_split_dyld_shared_cache(dmg: str, output_path: str) -> str:
    result = subprocess.run(["hdiutil", "mount", dmg], capture_output=True, check=True)

    mount = parse_hdiutil_mount_output(result.stdout.decode("utf-8"))
    image_name = mount.point.split("/").pop()
    split_dyld_cache_path = split_dyld_shared_cache(
        mount.point, output_path + "/" + image_name + "_libraries"
    )
    result = subprocess.run(["hdiutil", "detach", mount.dev], capture_output=True)
    print(f"\t\t\tResult from detach: {result}")

    return split_dyld_cache_path


def split_dyld_shared_cache(input_path, output_path):
    print(f"\t\tSplitting dyld_shared_cache of {input_path}")
    dyld_shared_cache_paths = find_dyld_shared_cache_path(input_path)
    split_dyld_cache_path = None
    for dyld_shared_cache_path in dyld_shared_cache_paths:
        split_dyld_cache_path = output_path
        result = subprocess.run(
            ["ipsw", "dyld", "split", dyld_shared_cache_path, split_dyld_cache_path],
            capture_output=True,
        )
        print(f"\t\t\tResult from split: {result}")
    return split_dyld_cache_path


def find_dyld_shared_cache_path(input_path: str) -> list[str]:
    # TODO: these options might be something that can be validated via meta-data
    dyld_shared_cache_arch_options = [
        "arm64e",
        "arm64",
        "arm64_32",
        "armv7k",
    ]

    # TODO: are we also interested in the DriverKit dyld_shared_cache?
    #  System/DriverKit/System/Library/dyld/
    dyld_shared_cache_path_prefix_options = [
        "/System/Library/Caches/com.apple.dyld/",
        "/AssetData/payloadv2/patches/System/Library/Caches/com.apple.dyld/",
        "/AssetData/payloadv2/ecc_data/System/Library/Caches/com.apple.dyld/",
    ]

    dyld_shared_cache_paths = []
    for path_prefix in dyld_shared_cache_path_prefix_options:
        for arch in dyld_shared_cache_arch_options:
            dyld_shared_cache_path = (
                input_path + path_prefix + "dyld_shared_cache_" + arch
            )
            if os.path.isfile(dyld_shared_cache_path):
                dyld_shared_cache_paths.append(dyld_shared_cache_path)

    if len(dyld_shared_cache_paths) == 0:
        raise RuntimeError(f"Couldn't find any dyld_shared_cache paths in {input_path}")
    elif len(dyld_shared_cache_paths) > 1:
        print(f"Found more than one dyld_shared_cache path in {input_path}")
        for path in dyld_shared_cache_paths:
            print(f"\t{path}")

    return dyld_shared_cache_paths


def symsort(
    dyld_shared_cache_split: str,
    output_path: str,
    platform: str,
    os_version: str,
    build_id: str,
    arch: str,
):
    print(f"\t\t\tSymsorting {dyld_shared_cache_split} to {output_path}")
    # TODO: the question here is whether we should write a common symsorter output directory
    symsort_output = output_path + "/symsorter_out"
    prefix = platform.lower()
    bundle_id = os_version + "_" + build_id + "_" + arch
    subprocess.run(
        [
            "./symsorter",
            "-zz",
            "-o",
            symsort_output,
            "--prefix",
            prefix,
            "--bundle-id",
            bundle_id,
            dyld_shared_cache_split,
        ],
        capture_output=True,
    )


def parse_path_prefix_from_dyld_shared_cache_extract_cmd(
    output: str, top_output_path: str
) -> str:
    for line in output.splitlines():
        top_output_path_index = line.find(top_output_path)
        if top_output_path_index == -1:
            continue

        extraction_name_start = top_output_path_index + len(top_output_path) + 1
        extraction_name_end = line.find("/", extraction_name_start)
        if extraction_name_end == -1:
            continue

        return top_output_path + "/" + line[extraction_name_start:extraction_name_end]

    raise RuntimeError(f"Couldn't find path_prefix in command-output: {output}")


def find_os_version_in_image_path(image_path: str) -> str:
    platform_version = pathlib.Path(image_path).parent.parts[-1][:-5]
    m = re.search(r"\d", platform_version)
    if m:
        version = platform_version[m.start() :]
        return version

    raise ValueError(f"Invalid image_path provided: {image_path}")


def extract_dyld_cache(image_path: str, output_path: str) -> bool:
    os_version = find_os_version_in_image_path(image_path)
    build_id = "19H218"
    arch = "arm64e"
    print(f"Trying patch_cryptex_dmg with {image_path}")
    extracted_dmgs = patch_cryptex_dmg(image_path, output_path)
    if len(extracted_dmgs) == 0:
        print(f"\tNot a cryptex, so extracting OTA dyld_shared_cache directly")

        dyld_shared_cache_top_output_path = output_path + "/dyld_shared_cache_output"
        # TODO: the output_path here should probably be a temp directory
        result = subprocess.run(
            [
                "ipsw",
                "ota",
                "extract",
                image_path,
                "dyld_shared_cache",
                "-o",
                dyld_shared_cache_top_output_path,
            ],
            capture_output=True,
        )
        if result.returncode == 1:
            # TODO: we must also differentiate here whether an image failed or whether it has no dyld_shared_cache that
            #  we can extract, because it is was a partial update file (or whatever). The latter should be marked as
            #  processed so we don't reprocess a partial update that will never be successful. May be irrelevant.
            print(f"\t\tFailed to extract dyld_shared_cache from: {image_path}")
            print(result.stderr.decode("utf-8"))
            return False
        else:
            print(f"\t\tSuccessfully extracted dyld_shared_cache from: {image_path}")
            extracted_dyld_shared_cache_path = (
                parse_path_prefix_from_dyld_shared_cache_extract_cmd(
                    result.stderr.decode("utf-8"), dyld_shared_cache_top_output_path
                )
            )
            print(f"\t\tSplitting & symsorting dyld_shared_cache for: {image_path}")
            symsort(
                split_dyld_shared_cache(extracted_dyld_shared_cache_path, output_path),
                output_path,
                "ios",
                os_version,
                build_id,
                arch,
            )
            return True
    else:
        # TODO: it is unclear whether 'cryptex-system-arm64e' always holds true
        # TODO: the output_path for mount & split should probably be a temp directory
        print(
            f"\tCryptex patch successful. Mount, split, symsorting dyld_shared_cache for: {image_path}"
        )
        split_cache_path = mount_and_split_dyld_shared_cache(
            extracted_dmgs["cryptex-system-arm64e"], output_path
        )
        symsort(
            split_cache_path,
            output_path,
            "ios",
            os_version,
            build_id,
            arch,
        )
        return True


def list_dyld_shared_cache_files(item):
    ps = subprocess.Popen(["ipsw", "ota", "ls", item], stdout=subprocess.PIPE)
    try:
        output = subprocess.check_output(("grep", "dyld_shared_cache"), stdin=ps.stdout)
        ps.wait()
        print(output.decode("utf-8"))
    except subprocess.CalledProcessError:
        print(f"no dyld_shared_cache found in {item}")


def log_as(name: str, item: str):
    with open(name, "a") as process_log_file:
        process_log_file.write(item + "\n")


def load_log(name: str) -> list[str]:
    try:
        with open(name) as process_log_file:
            return process_log_file.read().splitlines()
    except FileNotFoundError:
        return []


def gather_images_to_process(input_path: str) -> list[str]:
    new_images = scan_input_path(input_path)
    old_images = load_log("processed")
    old_images.extend(load_log("failed"))
    to_process = list(set(new_images) - set(old_images))
    return to_process


def main():
    args = parse_args()
    validate_shell_deps()

    to_process = gather_images_to_process(args.input_path)
    print(f"Processing {to_process}")

    if not os.path.isdir(args.output_path):
        os.mkdir(args.output_path)

    for item in to_process:
        # TODO: here we might want to store where a debug-id is coming from: like debug-id -> ota-image
        success = extract_dyld_cache(item, args.output_path)
        if success:
            log_as("processed", item)
        else:
            log_as("failed", item)


if __name__ == "__main__":
    main()
