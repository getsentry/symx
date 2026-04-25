from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

from symx.admin.db import IpswFailureRow, OtaFailureRow
from symx.download import try_download_url_to_file
from symx.fs import check_sha1

DOWNLOADS_DIR_NAME = "downloads"
StatusCallback = Callable[[str], None]


@dataclass(frozen=True)
class ArtifactDownloadResult:
    path: Path
    verified: bool
    message: str


class ArtifactDownloadError(RuntimeError):
    pass


def downloads_dir(cache_dir: Path) -> Path:
    return cache_dir / DOWNLOADS_DIR_NAME


def download_ipsw_to_cache(
    row: IpswFailureRow,
    cache_dir: Path,
    status_callback: StatusCallback | None = None,
) -> ArtifactDownloadResult:
    target_dir = downloads_dir(cache_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / _ipsw_download_file_name(row)

    if (
        row.sha1 is not None
        and target_path.exists()
        and check_sha1(row.sha1, target_path, status_callback=status_callback)
    ):
        return ArtifactDownloadResult(
            path=target_path,
            verified=True,
            message=f"IPSW already downloaded and SHA-1 verified: {target_path}",
        )

    temp_path = _temp_path_for(target_path)
    temp_path.unlink(missing_ok=True)
    try:
        try_download_url_to_file(row.link, temp_path, status_callback=status_callback)
        if row.sha1 is not None:
            _emit_status("Verifying IPSW SHA-1…", status_callback)
            if not check_sha1(row.sha1, temp_path, status_callback=status_callback):
                raise ArtifactDownloadError(f"Downloaded IPSW failed SHA-1 verification: {row.link}")
            verified = True
            message = f"Downloaded and SHA-1 verified: {target_path}"
        else:
            verified = False
            message = f"Downloaded without SHA verification (no sha1 in meta-data): {target_path}"
            _emit_status(message, status_callback)
        temp_path.replace(target_path)
        return ArtifactDownloadResult(path=target_path, verified=verified, message=message)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def download_ota_to_cache(
    row: OtaFailureRow,
    cache_dir: Path,
    status_callback: StatusCallback | None = None,
) -> ArtifactDownloadResult:
    target_dir = downloads_dir(cache_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / _ota_download_file_name(row)

    if (
        row.hash_algorithm == "SHA-1"
        and target_path.exists()
        and check_sha1(row.hash, target_path, status_callback=status_callback)
    ):
        return ArtifactDownloadResult(
            path=target_path,
            verified=True,
            message=f"OTA already downloaded and SHA-1 verified: {target_path}",
        )

    temp_path = _temp_path_for(target_path)
    temp_path.unlink(missing_ok=True)
    try:
        try_download_url_to_file(row.url, temp_path, status_callback=status_callback)
        if row.hash_algorithm == "SHA-1":
            _emit_status("Verifying OTA SHA-1…", status_callback)
            if not check_sha1(row.hash, temp_path, status_callback=status_callback):
                raise ArtifactDownloadError(f"Downloaded OTA failed SHA-1 verification: {row.url}")
            verified = True
            message = f"Downloaded and SHA-1 verified: {target_path}"
        else:
            verified = False
            message = (
                f"Downloaded without SHA verification (unsupported hash algorithm {row.hash_algorithm}): {target_path}"
            )
            _emit_status(message, status_callback)
        temp_path.replace(target_path)
        return ArtifactDownloadResult(path=target_path, verified=verified, message=message)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _emit_status(message: str, status_callback: StatusCallback | None) -> None:
    if status_callback is not None:
        status_callback(message)


def infer_file_name_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    direct_name = Path(parsed.path).name
    query = urllib.parse.parse_qs(parsed.query)
    query_path = query.get("path")
    if query_path:
        query_name = Path(query_path[0]).name
        if query_name:
            return query_name
    if direct_name:
        return direct_name
    return "download"


def _ipsw_download_file_name(row: IpswFailureRow) -> str:
    source_name = infer_file_name_from_url(row.link)
    link_hash = hashlib.sha1(row.link.encode("utf-8")).hexdigest()[:12]
    return _safe_name(f"{row.artifact_key}__{link_hash}__{source_name}")


def _ota_download_file_name(row: OtaFailureRow) -> str:
    source_name = infer_file_name_from_url(row.url)
    if not Path(source_name).suffix:
        source_name = f"{source_name}.zip"
    return _safe_name(f"{row.platform}_{row.version}_{row.build}_{row.artifact_id}__{source_name}")


def _safe_name(name: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_", "."} else "_" for character in name)


def _temp_path_for(path: Path) -> Path:
    return path.with_name(f"{path.name}.part")
