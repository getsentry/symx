import subprocess
from pathlib import Path

import common


def download_latest_ipsw_for(output_dir: Path, device: common.Device) -> None:
    """downloads latest IPSW for specific device"""
    cmd = [
        "ipsw",
        "download",
        "ipsw",
        "--output",
        str(output_dir),
        "-y",
        "--device",
        device.search_name,
        "--resume-all",
    ]
    subprocess.run(cmd)


def download_latest_macos_ipsw(output_dir: Path) -> None:
    """downloads latest IPSW for macOS"""
    cmd = [
        "ipsw",
        "download",
        "ipsw",
        "--latest",
        "--macos",
        "-y",
        "--output",
        str(output_dir),
        "--resume-all",
    ]
    subprocess.run(cmd)


def download_latest_ipsws(output_dir: Path) -> None:
    """downloads latest IPSW for all devices"""
    cmd = [
        "ipsw",
        "download",
        "ipsw",
        "--latest",
        "-y",
        "--output",
        str(output_dir),
        "--resume-all",
    ]
    subprocess.run(cmd)


def main() -> None:
    args = common.downloader_parse_args()
    common.downloader_validate_shell_deps()

    # prioritize latest for download
    download_latest_ipsws(args.output_dir)
    download_latest_macos_ipsw(args.output_dir)

    # then load by device (or release backwards?)
    device_list = common.ipsw_device_list()
    print(device_list)
    for device in device_list:
        download_latest_ipsw_for(args.output_dir, device)


if __name__ == "__main__":
    main()
