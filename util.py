import os
import re
import subprocess


def directory(path: str) -> str:
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
