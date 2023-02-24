import argparse
import glob
import os
import re
import shutil
import subprocess
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
    if result.returncode == 0 and result.stdout == b'' and result.stderr == b'':
        # TODO: zip files pre-ios16 produce no result when patching, what to do on those cases?
        print(f"This item is not cryptex encoded: {item}")
    else:
        for line in result.stderr.decode('utf-8').splitlines():
            re_match = re.search("Patching (.*) to (.*)", line)
            dmg_files[re_match.group(1)] = re_match.group(2)

    return dmg_files


def find_system_os_dmgs(path: str) -> list[str]:
    result = []
    for file_path in glob.iglob(path + "/**/SystemOS/*.dmg", recursive=True):
        result.append(file_path)
    return result


def mount_and_split_dyld_shared_cache(dmg: str, output_path: str) -> str:
    result = subprocess.run(["hdiutil", "mount", dmg], capture_output=True)
    if result.returncode == 0:
        mount_output = result.stdout.decode('utf-8').splitlines()
        mount_info = mount_output.pop().split()
        mount_dev = mount_info[0]
        mount_point = mount_info[2]
        image_name = mount_point.split('/').pop()

        print(f"Splitting dyld_shared_cache of {mount_point}")
        # TODO: this might be something that can be validated via meta-data
        dyld_shared_cache_arch_options = ["dyld_shared_cache_arm64e", "dyld_shared_cache_arm64"]
        found_dyld_shared_cache = False
        dyld_shared_cache_path = ""
        for option in dyld_shared_cache_arch_options:
            dyld_shared_cache_path = mount_point + "/System/Library/Caches/com.apple.dyld/" + option
            if os.path.isfile(dyld_shared_cache_path):
                found_dyld_shared_cache = True
                break

        split_dyld_cache_path = None
        if found_dyld_shared_cache:
            split_dyld_cache_path = output_path + "/" + image_name + "_libraries"
            result = subprocess.run(
                ["ipsw", "dyld", "split", dyld_shared_cache_path, split_dyld_cache_path],
                capture_output=True)
            print(f"Result from split: {result}")

        result = subprocess.run(["hdiutil", "detach", mount_dev], capture_output=True)
        print(f"Result from detach: {result}")

        return split_dyld_cache_path


def symsort(split_dyld_shared_cache: str, output_path: str):
    # TODO: the question here is whether we should write a common symsorter output directory
    symsort_output = output_path + "/symsorter_out"
    prefix = "ios"  # TODO: this should become a parameter which we can extract from the directory

    # TODO: this should become a parameter ->
    #  * we can get the first bundle_id parameter from the directory
    #  * the second can be found in the info.plist of the zip
    #  * the third can be extracted when searching for dyld_shared_cache
    bundle_id = "16.1.1_20B101_arm64e"
    subprocess.run(["./symsorter", "-zz", "-o", symsort_output, "--prefix", prefix, "--bundle-id", bundle_id,
                    split_dyld_shared_cache])


def extract_dyld_cache(item: str, output_path: str) -> bool:
    extracted_dmgs = patch_cryptex_dmg(item, output_path)
    if len(extracted_dmgs) == 0:
        # TODO: we have an image that is not cryptex encoded
        ps = subprocess.Popen(["ipsw", "ota", "ls", item], stdout=subprocess.PIPE)
        try:
            output = subprocess.check_output(('grep', 'dyld_shared_cache'), stdin=ps.stdout)
            ps.wait()
            print(output.decode('utf-8'))
        except subprocess.CalledProcessError:
            print(f"no dyld_shared_cache found in {item}")

        subprocess.run(["ipsw", "ota", "extract", item, "'^System.*'"])

        return False
    else:
        split_dyld_shared_cache = mount_and_split_dyld_shared_cache(extracted_dmgs['cryptex-system-arm64e'],
                                                                    output_path)
        symsort(split_dyld_shared_cache, output_path)
        return True


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

    # TODO: maybe instead of providing an output path we should manage this ourselves (i.e. run-path using uuid)
    shutil.rmtree(args.output_path)
    os.mkdir(args.output_path)
    for item in to_process:
        success = extract_dyld_cache(item, args.output_path)
        if success:
            store_as_processed(item)


if __name__ == '__main__':
    main()
