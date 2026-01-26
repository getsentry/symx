"""
Tests for OTA metadata parsing from ipsw command output.
"""

import json
import subprocess

from symx._common import ArtifactProcessingState
from symx._ota import OtaMetaData, parse_download_meta_output


def make_completed_process(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_parse_download_meta_output_success() -> None:
    """Parse successful ipsw download ota output."""
    meta_json = [
        {
            "url": "https://updates.apple.com/abc123def456789012345678901234567890.zip",
            "build": "21A100",
            "version": "17.0",
            "hash": "somehash",
            "hash_algorithm": "SHA-1",
            "devices": ["iPhone14,7"],
            "description": "iOS 17.0",
        }
    ]
    result = make_completed_process(stdout=json.dumps(meta_json).encode())
    meta: OtaMetaData = {}

    parse_download_meta_output("ios", result, meta, beta=False)

    assert "abc123def456789012345678901234567890" in meta
    artifact = meta["abc123def456789012345678901234567890"]
    assert artifact.build == "21A100"
    assert artifact.version == "17.0"
    assert artifact.platform == "ios"
    assert artifact.devices == ["iPhone14,7"]
    assert artifact.description == ["iOS 17.0"]
    assert artifact.processing_state == ArtifactProcessingState.INDEXED


def test_parse_download_meta_output_beta_suffix() -> None:
    """Beta artifacts get _beta suffix in key."""
    meta_json = [
        {
            "url": "https://updates.apple.com/abc123def456789012345678901234567890.zip",
            "build": "21A5100a",
            "version": "17.0",
            "hash": "somehash",
            "hash_algorithm": "SHA-1",
        }
    ]
    result = make_completed_process(stdout=json.dumps(meta_json).encode())
    meta: OtaMetaData = {}

    parse_download_meta_output("ios", result, meta, beta=True)

    assert "abc123def456789012345678901234567890_beta" in meta
    assert "abc123def456789012345678901234567890" not in meta


def test_parse_download_meta_output_no_description() -> None:
    """Handle missing description field."""
    meta_json = [
        {
            "url": "https://updates.apple.com/abc123def456789012345678901234567890.zip",
            "build": "21A100",
            "version": "17.0",
            "hash": "h",
            "hash_algorithm": "SHA-1",
        }
    ]
    result = make_completed_process(stdout=json.dumps(meta_json).encode())
    meta: OtaMetaData = {}

    parse_download_meta_output("ios", result, meta, beta=False)

    assert meta["abc123def456789012345678901234567890"].description == []


def test_parse_download_meta_output_no_devices() -> None:
    """Handle missing devices field."""
    meta_json = [
        {
            "url": "https://updates.apple.com/abc123def456789012345678901234567890.zip",
            "build": "21A100",
            "version": "17.0",
            "hash": "h",
            "hash_algorithm": "SHA-1",
        }
    ]
    result = make_completed_process(stdout=json.dumps(meta_json).encode())
    meta: OtaMetaData = {}

    parse_download_meta_output("ios", result, meta, beta=False)

    assert meta["abc123def456789012345678901234567890"].devices == []


def test_parse_download_meta_output_sha256_id() -> None:
    """Handle SHA256 zip IDs (64 chars)."""
    sha256_id = "a" * 64
    meta_json = [
        {
            "url": f"https://updates.apple.com/{sha256_id}.zip",
            "build": "21A100",
            "version": "17.0",
            "hash": "h",
            "hash_algorithm": "SHA-1",
        }
    ]
    result = make_completed_process(stdout=json.dumps(meta_json).encode())
    meta: OtaMetaData = {}

    parse_download_meta_output("ios", result, meta, beta=False)

    assert sha256_id in meta


def test_parse_download_meta_output_failure_ignored() -> None:
    """Non-zero return code doesn't crash, just logs."""
    result = make_completed_process(returncode=1, stderr=b"some error")
    meta: OtaMetaData = {}

    parse_download_meta_output("ios", result, meta, beta=False)

    assert len(meta) == 0


def test_parse_download_meta_output_403_silently_ignored() -> None:
    """403 errors are common and shouldn't be logged as errors."""
    result = make_completed_process(returncode=1, stderr=b"api returned status: 403 Forbidden")
    meta: OtaMetaData = {}

    # Should not raise, should not log error (we can't easily test logging here)
    parse_download_meta_output("ios", result, meta, beta=False)

    assert len(meta) == 0


def test_parse_download_meta_output_multiple_artifacts() -> None:
    """Parse multiple artifacts from single response."""
    meta_json = [
        {
            "url": f"https://updates.apple.com/{'a' * 40}.zip",
            "build": "21A100",
            "version": "17.0",
            "hash": "h1",
            "hash_algorithm": "SHA-1",
        },
        {
            "url": f"https://updates.apple.com/{'b' * 40}.zip",
            "build": "21A101",
            "version": "17.0.1",
            "hash": "h2",
            "hash_algorithm": "SHA-1",
        },
    ]
    result = make_completed_process(stdout=json.dumps(meta_json).encode())
    meta: OtaMetaData = {}

    parse_download_meta_output("ios", result, meta, beta=False)

    assert len(meta) == 2
    assert "a" * 40 in meta
    assert "b" * 40 in meta
