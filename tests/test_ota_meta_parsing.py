"""Tests for OTA metadata parsing from ``ipsw download ota --json`` output."""

import json
import logging
import subprocess
import threading
import time
from collections.abc import Generator
from datetime import UTC
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from symx.diagnostics import MAX_SUBPROCESS_OUTPUT_CHARS
from symx.model import ArtifactProcessingState
from symx.ota.meta import _download_meta_job, parse_download_meta_output, retrieve_current_meta
from symx.ota.model import OtaMetaData


def make_completed_process(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def ota_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "url": "https://updates.apple.com/abc123def456789012345678901234567890.zip",
        "build": "21A100",
        "version": "17.0",
        "sha1": "somehash",
        "sha256": "ignored-sha256",
        "delivery": "full",
        "supported_devices": ["iPhone14,7"],
        "supported_models": ["D27AP"],
    }
    item.update(overrides)
    return item


def ota_envelope(*items: dict[str, object], schema_version: int = 1) -> dict[str, object]:
    return {"schema_version": schema_version, "otas": list(items)}


def parse_payload(payload: object, *, platform: str = "ios", beta: bool = False) -> OtaMetaData:
    result = make_completed_process(stdout=json.dumps(payload).encode())
    meta: OtaMetaData = {}
    parse_download_meta_output(platform, result, meta, beta)
    return meta


def test_parse_download_meta_output_success() -> None:
    meta = parse_payload(
        ota_envelope(
            ota_item(
                channel={"release_type": "Darwin Recovery", "documentation_id": "iOS 17.0"},
                delivery="delta",
                prerequisite={"build": "20A99", "version": "16.6"},
                provenance={"asset_type": "com.apple.MobileAsset.RecoveryOSUpdate"},
            )
        )
    )

    artifact = meta["abc123def456789012345678901234567890"]
    assert artifact.build == "21A100"
    assert artifact.version == "17.0"
    assert artifact.platform == "ios"
    assert artifact.devices == ["iPhone14,7"]
    assert artifact.supported_models == ["D27AP"]
    assert artifact.description == ["iOS 17.0"]
    assert artifact.hash == "somehash"
    assert artifact.hash_algorithm == "SHA-1"
    assert artifact.release_type == "Darwin Recovery"
    assert artifact.asset_type == "com.apple.MobileAsset.RecoveryOSUpdate"
    assert artifact.delivery == "delta"
    assert artifact.prerequisite_build == "20A99"
    assert artifact.prerequisite_version == "16.6"
    assert artifact.processing_state == ArtifactProcessingState.INDEXED
    assert artifact.last_modified is not None
    assert artifact.last_modified.tzinfo is UTC


def test_parse_download_meta_output_beta_suffix() -> None:
    meta = parse_payload(ota_envelope(ota_item(build="21A5100a")), beta=True)

    assert "abc123def456789012345678901234567890_beta" in meta
    assert "abc123def456789012345678901234567890" not in meta


def test_parse_download_meta_output_accepts_absent_and_null_optional_metadata() -> None:
    without_optional = ota_item()
    without_optional.pop("supported_devices")
    without_optional.pop("supported_models")
    null_optional = ota_item(
        url=f"https://updates.apple.com/{'b' * 40}.aea",
        channel=None,
        prerequisite=None,
        provenance=None,
    )

    meta = parse_payload(ota_envelope(without_optional, null_optional))

    artifact = meta["abc123def456789012345678901234567890"]
    assert artifact.devices == []
    assert artifact.supported_models == []
    assert artifact.description == []
    assert artifact.release_type is None
    assert artifact.asset_type is None
    assert artifact.prerequisite_build is None
    assert artifact.prerequisite_version is None
    assert "b" * 40 in meta


def test_parse_download_meta_output_sha256_id() -> None:
    sha256_id = "a" * 64

    meta = parse_payload(ota_envelope(ota_item(url=f"https://updates.apple.com/{sha256_id}.zip")))

    assert sha256_id in meta


@pytest.mark.parametrize("value", [None, "", "absent"])
def test_parse_download_meta_output_requires_sha1(value: object) -> None:
    item = ota_item()
    if value == "absent":
        item.pop("sha1")
    else:
        item["sha1"] = value

    with pytest.raises(ValidationError, match="sha1"):
        parse_payload(ota_envelope(item))


@pytest.mark.parametrize("field", ["url", "build", "version", "delivery"])
@pytest.mark.parametrize("value", [pytest.param(None, id="null"), pytest.param("absent", id="absent")])
def test_parse_download_meta_output_rejects_null_or_absent_required_item_fields(
    field: str,
    value: object,
) -> None:
    item = ota_item()
    if value == "absent":
        item.pop(field)
    else:
        item[field] = value

    with pytest.raises(ValidationError):
        parse_payload(ota_envelope(item))


def test_parse_download_meta_output_rejects_unsupported_schema_explicitly() -> None:
    with pytest.raises(ValueError, match="Unsupported ipsw OTA metadata schema version: 2"):
        parse_payload(ota_envelope(ota_item(), schema_version=2))


@pytest.mark.parametrize("payload", [[ota_item()], {"schema_version": 1}, {"schema_version": 1, "otas": None}])
def test_parse_download_meta_output_requires_schema_1_envelope(payload: object) -> None:
    with pytest.raises(ValidationError):
        parse_payload(payload)


def test_parse_download_meta_output_failure_logs_bounded_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    stderr = b"failure-start\n" + (b"x" * MAX_SUBPROCESS_OUTPUT_CHARS) + b"\nfailure-end"
    result = make_completed_process(returncode=7, stderr=stderr)
    meta: OtaMetaData = {}

    with caplog.at_level(logging.ERROR, logger="symx.ota.meta"):
        parse_download_meta_output("tvos", result, meta, beta=True)

    assert not meta
    assert "Download OTA meta failed for tvos (beta) (exit 7)" in caplog.text
    assert "failure-start" in caplog.text
    assert "[truncated" in caplog.text
    assert "failure-end" not in caplog.text


def test_parse_download_meta_output_403_silently_ignored(caplog: pytest.LogCaptureFixture) -> None:
    result = make_completed_process(returncode=1, stderr=b"api returned status: 403 Forbidden")
    meta: OtaMetaData = {}

    with caplog.at_level(logging.ERROR, logger="symx.ota.meta"):
        parse_download_meta_output("ios", result, meta, beta=False)

    assert not meta
    assert not caplog.records


def test_parse_download_meta_output_preserves_content_aliases() -> None:
    meta = parse_payload(
        ota_envelope(
            ota_item(version="9.3.5", build="13G36"),
            ota_item(version="7.1.2", build="11D257", delivery="delta"),
        )
    )

    artifact_id = "abc123def456789012345678901234567890"
    assert meta[artifact_id].version == "9.3.5"
    assert meta[f"{artifact_id}_duplicate_1"].version == "7.1.2"
    assert meta[f"{artifact_id}_duplicate_1"].processing_state == ArtifactProcessingState.INDEXED_DUPLICATE


def test_parse_download_meta_output_multiple_artifacts() -> None:
    meta = parse_payload(
        ota_envelope(
            ota_item(url=f"https://updates.apple.com/{'a' * 40}.zip", sha1="h1"),
            ota_item(
                url=f"https://updates.apple.com/{'b' * 40}.zip",
                build="21A101",
                version="17.0.1",
                sha1="h2",
            ),
        )
    )

    assert len(meta) == 2
    assert "a" * 40 in meta
    assert "b" * 40 in meta


def test_download_meta_job_records_bounded_subprocess_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    span_data: dict[str, object] = {}

    class RecordingSpan:
        def set_data(self, key: str, value: object) -> None:
            span_data[key] = value

    @contextmanager
    def recording_span(*args: object, **kwargs: object) -> Generator[RecordingSpan]:
        assert kwargs == {
            "op": "subprocess.ipsw_download_meta",
            "name": "Fetch OTA meta for tvos (beta)",
        }
        yield RecordingSpan()

    stderr = b"failure-start\n" + (b"x" * MAX_SUBPROCESS_OUTPUT_CHARS) + b"\nfailure-end"

    def fake_run(cmd: list[str], capture_output: bool = False) -> subprocess.CompletedProcess[bytes]:
        assert cmd == ["ipsw", "download", "ota", "--platform", "tvos", "--json", "--beta"]
        assert capture_output is True
        return make_completed_process(returncode=7, stderr=stderr)

    monkeypatch.setattr("symx.ota.meta.sentry_sdk.start_span", recording_span)
    monkeypatch.setattr("symx.ota.meta.subprocess.run", fake_run)

    assert _download_meta_job(("tvos", True)) == {}
    assert span_data["platform"] == "tvos"
    assert span_data["beta"] is True
    assert span_data["command"] == "ipsw download ota --platform tvos --json --beta"
    assert span_data["returncode"] == 7
    assert isinstance(span_data["stderr"], str)
    assert "failure-start" in span_data["stderr"]
    assert "[truncated" in span_data["stderr"]
    assert "failure-end" not in span_data["stderr"]


def test_retrieve_current_meta_fetches_all_platform_variants_in_parallel_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platforms = ["ios", "watchos", "tvos"]
    monkeypatch.setattr("symx.ota.meta.PLATFORMS", platforms)

    release_id = "a" * 40
    beta_id = "b" * 40
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def fake_run(cmd: list[str], capture_output: bool = False) -> subprocess.CompletedProcess[bytes]:
        assert capture_output is True
        platform = cmd[cmd.index("--platform") + 1]
        beta = "--beta" in cmd

        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)

        time.sleep(0.05)

        try:
            artifact_id = beta_id if beta else release_id
            payload = ota_envelope(
                ota_item(
                    url=f"https://updates.apple.com/{artifact_id}.zip",
                    build=f"{platform}-{'beta' if beta else 'release'}",
                    sha1=f"{platform}-{'beta' if beta else 'release'}",
                )
            )
            return make_completed_process(stdout=json.dumps(payload).encode())
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr("symx.ota.meta.subprocess.run", fake_run)

    meta = retrieve_current_meta()

    assert max_active > 1
    assert meta[release_id].platform == "tvos"
    assert meta[release_id].build == "tvos-release"
    assert meta[f"{beta_id}_beta"].platform == "tvos"
    assert meta[f"{beta_id}_beta"].build == "tvos-beta"
