import os
import re
import subprocess


def directory(path: str) -> str:
    if not os.path.isdir(path):
        raise ValueError(f"Error: {path} is not a valid directory")
    else:
        return path


def ipsw_version() -> str | None:
    result = subprocess.run(["ipsw", "version"], capture_output=True)
    if result.returncode == 0:
        output = result.stdout.decode("utf-8")
        match = re.search("Version: (.*),", output)
        version = match.group(1)
        return version
