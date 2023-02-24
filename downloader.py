import argparse
import subprocess
from argparse import Namespace

import util


def download_otas(output_path: str, platform: str):
    subprocess.run(["ipsw", "download", "ota", "--output", output_path, "-y", "--platform", platform, "--resume-all"])
    # TODO: we want beta releases too


def parse_args() -> Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output_path', dest='output_path', required=True, type=util.directory,
                        help='path to the output directory where the extracted symbols are placed')
    return parser.parse_args()


def validate_shell_deps():
    # TODO: check for ipsw
    pass


def main():
    args = parse_args()
    validate_shell_deps()
    for platform in ["ios", "watchos", "tvos", "audioos", "accessory", "macos", "recovery"]:
        download_otas(args.output_path, platform)


if __name__ == '__main__':
    main()
