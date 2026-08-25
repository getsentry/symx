"""Compressed directory archives used to bound extraction disk usage."""

import shutil
import subprocess
from pathlib import Path


class DirectoryArchiveError(Exception):
    """Raised when a directory cannot be compressed or restored."""


def compress_directory(directory: Path) -> Path:
    """Compress ``directory`` beside itself and remove it after success."""
    archive_path = directory.parent / f"{directory.name}.tar.zst"

    with subprocess.Popen(
        ["tar", "-cf", "-", "-C", str(directory.parent), directory.name],
        stdout=subprocess.PIPE,
    ) as tar_proc:
        with subprocess.Popen(
            ["zstd", "-", "-o", str(archive_path)],
            stdin=tar_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as zstd_proc:
            if tar_proc.stdout:
                tar_proc.stdout.close()
            _, stderr = zstd_proc.communicate()

            if zstd_proc.returncode != 0:
                error_msg = stderr.decode("utf-8") if stderr else "Unknown error"
                raise DirectoryArchiveError(f"zstd compression failed: {error_msg}")

    if tar_proc.returncode != 0:
        raise DirectoryArchiveError(f"tar archiving failed with return code {tar_proc.returncode}")

    shutil.rmtree(directory)
    return archive_path


def decompress_archive(archive_path: Path, target_dir: Path) -> None:
    """Restore an archive created by :func:`compress_directory`."""
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    with subprocess.Popen(["zstd", "-d", str(archive_path), "-c"], stdout=subprocess.PIPE) as zstd_proc:
        with subprocess.Popen(
            ["tar", "-xf", "-", "-C", str(target_dir.parent)],
            stdin=zstd_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as tar_proc:
            if zstd_proc.stdout:
                zstd_proc.stdout.close()
            _, stderr = tar_proc.communicate()

            if tar_proc.returncode != 0:
                error_msg = stderr.decode("utf-8") if stderr else "Unknown error"
                raise DirectoryArchiveError(f"tar extraction failed: {error_msg}")

    if zstd_proc.returncode != 0:
        raise DirectoryArchiveError(f"zstd decompression failed with return code {zstd_proc.returncode}")
