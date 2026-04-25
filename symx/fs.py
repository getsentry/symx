"""Filesystem utilities and hash verification."""

import hashlib
import logging
import shutil
from collections.abc import Callable
from math import floor
from pathlib import Path

import sentry_sdk
import sentry_sdk.metrics

from symx.model import HASH_BLOCK_SIZE, MiB

logger = logging.getLogger(__name__)

StatusCallback = Callable[[str], None]


def check_sha1(hash_sum: str, filepath: Path, status_callback: StatusCallback | None = None) -> bool:
    sha1sum = hashlib.sha1()
    with open(filepath, "rb") as f:
        block = f.read(HASH_BLOCK_SIZE)
        while block:
            sha1sum.update(block)
            block = f.read(HASH_BLOCK_SIZE)

    sha1sum_result = sha1sum.hexdigest()
    _emit_status(f"Calculated sha1 {sha1sum_result} (expected {hash_sum})", status_callback)
    return sha1sum_result == hash_sum


def _emit_status(message: str, status_callback: StatusCallback | None) -> None:
    if status_callback is not None:
        status_callback(message)
        return
    logger.info(message)


def list_dirs_in(dir_path: Path) -> list[Path]:
    if dir_path.exists() and dir_path.is_dir():
        return [entry for entry in dir_path.iterdir() if entry.is_dir()]
    else:
        raise ValueError("The provided path does not exist or is not a directory.")


def rmdir_if_exists(dir_path: Path) -> None:
    if dir_path.exists() and dir_path.is_dir():
        shutil.rmtree(dir_path)


def log_disk_usage(prefix: str = "") -> None:
    total, used, free = shutil.disk_usage("/")
    free_mib = free / MiB
    used_pct = (used / total) * 100 if total > 0 else 0
    if prefix:
        logger.info("disk_usage (%s): %.0f%% used, %dMiB free", prefix, used_pct, floor(free_mib))
    else:
        logger.info("disk_usage: %.0f%% used, %dMiB free", used_pct, floor(free_mib))
    sentry_sdk.metrics.gauge("disk.free_bytes", free, unit="byte")
    sentry_sdk.metrics.gauge("disk.used_percent", used_pct, unit="ratio")
