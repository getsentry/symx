import os


def directory(path: str) -> str:
    if not os.path.isdir(path):
        raise ValueError(f"Error: {path} is not a valid directory")
    else:
        return path
