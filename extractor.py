import argparse
import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Union

import util


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input_path', dest='input_path', required=True, type=util.directory,
                        help='path to the input directory that is scanned for images to extract symbols from')
    parser.add_argument('--output_path', dest='output_path', required=True, type=util.directory,
                        help='path to the output directory where the extracted symbols are placed')
    return parser.parse_args()


def validate_shell_deps():
    # TODO: check/download for symsorter
    # TODO: check for ipsw
    pass


def scan_input_path(input_path: Union[str, os.PathLike]) -> list[str]:
    result = []
    for file_path in glob.iglob(input_path + "/**/*.zip", recursive=True):
        result.append(file_path)
    return result


def patch_cryptex_dmg(item: str, output_path: str) -> dict[str, str]:
    dmg_files = {}
    result = subprocess.run(["ipsw", "ota", "patch", item, "--output", output_path], capture_output=True)
    if result.returncode == 0 and result.stderr != b'':
        for line in result.stderr.decode('utf-8').splitlines():
            re_match = re.search("Patching (.*) to (.*)", line)
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
    result = subprocess.run(["hdiutil", "mount", dmg], capture_output=True)
    if result.returncode == 0:
        mount = parse_hdiutil_mount_output(result.stdout.decode('utf-8'))
        image_name = mount.point.split('/').pop()
        split_dyld_cache_path = split_dyld_shared_cache(mount.point, output_path + "/" + image_name + "_libraries")
        result = subprocess.run(["hdiutil", "detach", mount.dev], capture_output=True)
        print(f"Result from detach: {result}")
        return split_dyld_cache_path


def split_dyld_shared_cache(input_path, output_path):
    print(f"Splitting dyld_shared_cache of {input_path}")
    dyld_shared_cache_paths = find_dyld_shared_cache_path(input_path)
    split_dyld_cache_path = None
    for dyld_shared_cache_path in dyld_shared_cache_paths:
        split_dyld_cache_path = output_path
        result = subprocess.run(
            ["ipsw", "dyld", "split", dyld_shared_cache_path, split_dyld_cache_path],
            capture_output=True)
        print(f"Result from split: {result}")
    return split_dyld_cache_path


def find_dyld_shared_cache_path(input_path: str) -> list[str]:
    # TODO: these options might be something that can be validated via meta-data
    dyld_shared_cache_arch_options = ["dyld_shared_cache_arm64e", "dyld_shared_cache_arm64",
                                      "dyld_shared_cache_arm64_32", "dyld_shared_cache_armv7k"]

    # TODO: are we also interested in the DriverKit dyld_shared_cache?
    #  System/DriverKit/System/Library/dyld/
    dyld_shared_cache_path_prefix_options = ["/System/Library/Caches/com.apple.dyld/",
                                             "/AssetData/payloadv2/patches/System/Library/Caches/com.apple.dyld/",
                                             "/AssetData/payloadv2/ecc_data/System/Library/Caches/com.apple.dyld/"]

    dyld_shared_cache_paths = []
    for path_prefix in dyld_shared_cache_path_prefix_options:
        for arch in dyld_shared_cache_arch_options:
            dyld_shared_cache_path = input_path + path_prefix + arch
            if os.path.isfile(dyld_shared_cache_path):
                dyld_shared_cache_paths.append(dyld_shared_cache_path)

    return dyld_shared_cache_paths


def symsort(dyld_shared_cache_split: str, output_path: str):
    # TODO: the question here is whether we should write a common symsorter output directory
    symsort_output = output_path + "/symsorter_out"
    prefix = "ios"  # TODO: this should become a parameter which we can extract from the directory

    # TODO: this should become a parameter ->
    #  * we can get the first bundle_id parameter from the directory
    #  * the second can be found in the info.plist of the zip
    #  * the third can be extracted when searching for dyld_shared_cache
    bundle_id = "16.1.1_20B101_arm64e"
    subprocess.run(["./symsorter", "-zz", "-o", symsort_output, "--prefix", prefix, "--bundle-id", bundle_id,
                    dyld_shared_cache_split], capture_output=True)


def parse_extracted_dyld_shared_cache_path_prefix(output: str, top_output_path: str) -> str:
    for line in output.splitlines():
        top_output_path_index = line.find(top_output_path)
        if top_output_path_index == -1: continue

        extraction_name_start = top_output_path_index + len(top_output_path) + 1
        extraction_name_end = line.find('/', extraction_name_start)
        if extraction_name_end == -1: continue

        return top_output_path + "/" + line[extraction_name_start: extraction_name_end]


def extract_dyld_cache(item: str, output_path: str) -> bool:
    print(f"Trying patch_cryptex_dmg with {item}")
    extracted_dmgs = patch_cryptex_dmg(item, output_path)
    if len(extracted_dmgs) == 0:
        print(f"\tNot a cryptex, so extracting OTA dyld_shared_cache directly")

        dyld_shared_cache_top_output_path = output_path + "/dyld_shared_cache_output"
        # TODO: the output_path here should probably be a temp directory
        result = subprocess.run(["ipsw", "ota", "extract", item, "dyld_shared_cache", "-o",
                                 dyld_shared_cache_top_output_path],
                                capture_output=True)
        if result.returncode == 1:
            # TODO: we must also differentiate here whether an image failed or whether it has no dyld_shared_cache that
            #  we can extract, because it is was a partial update file (or whatever). The latter should be marked as
            #  processed so we don't reprocess a partial update that will never be successful. May be irrelevant.
            print(f"\t\tFailed to extract dyld_shared_cache from: {item}")
            print(result.stderr.decode('utf-8'))
            return False
        else:
            print(f"\t\tSuccessfully extracted dyld_shared_cache from: {item}")
            extracted_dyld_shared_cache_path = parse_extracted_dyld_shared_cache_path_prefix(
                result.stderr.decode('utf-8'), dyld_shared_cache_top_output_path)
            print(f"\t\tSplitting & symsorting dyld_shared_cache for: {item}")
            symsort(split_dyld_shared_cache(extracted_dyld_shared_cache_path, output_path), output_path)
            return True
    else:
        # TODO: it is unclear whether 'cryptex-system-arm64e' always holds true
        # TODO: the output_path for mount & split should probably be a temp directory
        print(f"\tCryptex patch successful. Mount, split, symsorting dyld_shared_cache for: {item}")
        symsort(mount_and_split_dyld_shared_cache(extracted_dmgs['cryptex-system-arm64e'], output_path), output_path)
        return True


def list_dyld_shared_cache_files(item):
    ps = subprocess.Popen(["ipsw", "ota", "ls", item], stdout=subprocess.PIPE)
    try:
        output = subprocess.check_output(('grep', 'dyld_shared_cache'), stdin=ps.stdout)
        ps.wait()
        print(output.decode('utf-8'))
    except subprocess.CalledProcessError:
        print(f"no dyld_shared_cache found in {item}")


def store_as_processed(item: str):
    with open("processed", "a") as processed_file:
        processed_file.write(item + '\n')


def load_processed() -> list[str]:
    try:
        with open("processed") as processed_file:
            return processed_file.read().splitlines()
    except FileNotFoundError:
        return []


def main():
    args = parse_args()
    validate_shell_deps()
    new_images = scan_input_path(args.input_path)
    old_images = load_processed()
    to_process = list(set(new_images) - set(old_images))
    print(f"Processing {to_process}")

    # TODO: maybe instead of providing an output path we should manage this ourselves (i.e. run-path using uuid)
    shutil.rmtree(args.output_path)
    os.mkdir(args.output_path)
    for item in to_process:
        success = extract_dyld_cache(item, args.output_path)
        if success:
            store_as_processed(item)


if __name__ == '__main__':
    main()
