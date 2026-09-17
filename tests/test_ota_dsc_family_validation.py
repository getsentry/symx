"""Family selection must not depend on ipsw suppressing excluded failures."""

import json
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from symx.model import Arch
from symx.ota.extract import extract_ota
from symx.ota.model.materialization import (
    OtaDscMaterializationRequest,
    OtaDscMaterializationResult,
    OtaDscProtocolError,
    OtaDscSource,
    OtaDscUnavailable,
    OtaDscUnavailableReason,
)

SYSTEM = "24J361__AppleTV14,1/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64e"
DRIVERKIT = "24J361__AppleTV14,1/System/DriverKit/System/Library/dyld/dyld_shared_cache_arm64e"
X86SUPPORT = "24J361__AppleTV14,1/System/x86Support/System/Library/dyld/dyld_shared_cache_x86_64"
UNKNOWN = "24J361__AppleTV14,1/Other/System/Library/dyld/dyld_shared_cache_arm64e"


@pytest.fixture
def request_context(tmp_path: Path) -> OtaDscMaterializationRequest:
    return OtaDscMaterializationRequest(
        local_ota=tmp_path / "unused.aea",
        output_root=tmp_path / "materialized",
        platform="tvos",
        version="27.0",
        build="24J361",
        bundle_id="family-test",
    )


def mock_report(
    request: OtaDscMaterializationRequest,
    monkeypatch: pytest.MonkeyPatch,
    paths: list[str],
    failures: list[str | None],
    *,
    phase: str = "dsc-validation",
    source: str = "payloadv2",
    complete: bool = False,
    returncode: int = 1,
) -> bytes:
    files: list[dict[str, str]] = []

    for path in paths:
        artifact = request.output_root / path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.touch()
        arch = artifact.name.removeprefix("dyld_shared_cache_").split(".", 1)[0]
        files.append({"path": path, "arch": arch, "source": "payloadv2"})

    errors: list[dict[str, str]] = []

    for path in failures:
        error = {"phase": phase, "source": source, "message": "diagnostic only, not a family identifier"}

        if path is not None:
            error["path"] = path

        errors.append(error)

    raw = json.dumps({"schema_version": 1, "complete": complete, "files": files, "errors": errors}).encode()

    monkeypatch.setattr(
        "symx.ota.extract.subprocess.run",
        lambda args, **kwargs: CompletedProcess(args, returncode, raw, b""),
    )

    return raw


@pytest.mark.parametrize(
    ("paths", "failures", "reason"),
    [
        ([SYSTEM, DRIVERKIT], [DRIVERKIT], None),
        ([SYSTEM, DRIVERKIT + ".01"], [DRIVERKIT], None),
        ([SYSTEM, X86SUPPORT], [X86SUPPORT], None),
        ([SYSTEM, DRIVERKIT, X86SUPPORT], [DRIVERKIT, X86SUPPORT], None),
        ([SYSTEM, DRIVERKIT], [SYSTEM], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM, DRIVERKIT], [SYSTEM, DRIVERKIT], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM, DRIVERKIT], [None], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM, DRIVERKIT], [DRIVERKIT + ".01"], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM], [DRIVERKIT], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM, UNKNOWN], [UNKNOWN], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM, DRIVERKIT], ["/" + DRIVERKIT], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM, DRIVERKIT], ["../" + DRIVERKIT], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM, DRIVERKIT], [DRIVERKIT.replace("/", "\\")], OtaDscUnavailableReason.INCOMPLETE),
        ([SYSTEM, DRIVERKIT], [DRIVERKIT.replace("arm64e", "x86_64")], OtaDscUnavailableReason.INCOMPLETE),
        (
            [SYSTEM, DRIVERKIT.replace("arm64e", "future_arch")],
            [DRIVERKIT.replace("arm64e", "future_arch")],
            OtaDscUnavailableReason.INCOMPLETE,
        ),
        ([DRIVERKIT], [DRIVERKIT], OtaDscUnavailableReason.NO_SUPPORTED_PRIMARY),
    ],
)
def test_materialization_selects_families_without_hiding_required_failures(
    request_context: OtaDscMaterializationRequest,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    paths: list[str],
    failures: list[str | None],
    reason: OtaDscUnavailableReason | None,
) -> None:
    raw = mock_report(request_context, monkeypatch, paths, failures)

    result = extract_ota(request_context)

    if reason is None:
        assert isinstance(result, OtaDscMaterializationResult)
        assert result.dscs == (OtaDscSource(arch=Arch.ARM64E, artifact=request_context.output_root / SYSTEM),)

        for path in failures:
            assert path is not None and path in caplog.text

        assert "excluded" in caplog.text
    else:
        assert isinstance(result, OtaDscUnavailable)
        assert result.reason == reason
        # Preserve the complete upstream report, including excluded diagnostics.
        assert json.loads(result.report.model_dump_json(exclude_none=True)) == json.loads(raw)


@pytest.mark.parametrize("phase", ["payload-extract", "copy", "mount", "cleanup", "future-phase"])
def test_excluded_family_does_not_hide_other_failure_phases(
    request_context: OtaDscMaterializationRequest, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    mock_report(request_context, monkeypatch, [SYSTEM, DRIVERKIT], [DRIVERKIT], phase=phase)

    result = extract_ota(request_context)

    assert isinstance(result, OtaDscUnavailable)
    assert result.reason == OtaDscUnavailableReason.INCOMPLETE


def test_excluded_validation_does_not_hide_mixed_extraction_errors(
    request_context: OtaDscMaterializationRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = mock_report(request_context, monkeypatch, [SYSTEM, DRIVERKIT], [DRIVERKIT])
    report = json.loads(raw)
    report["errors"].append({"phase": "payload-extract", "source": "payload.026", "message": "failed"})
    monkeypatch.setattr(
        "symx.ota.extract.subprocess.run",
        lambda args, **kwargs: CompletedProcess(args, 1, json.dumps(report).encode(), b""),
    )

    result = extract_ota(request_context)

    assert isinstance(result, OtaDscUnavailable)
    assert result.reason == OtaDscUnavailableReason.INCOMPLETE
    assert not result.has_only_dsc_validation_failures


def test_family_failure_must_match_reported_source(
    request_context: OtaDscMaterializationRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_report(request_context, monkeypatch, [SYSTEM, DRIVERKIT], [DRIVERKIT], source="cryptex-system-arm64e")

    assert isinstance(extract_ota(request_context), OtaDscUnavailable)


@pytest.mark.parametrize(("complete", "returncode"), [(True, 0), (False, 0)])
def test_excluded_failures_do_not_bypass_report_process_contract(
    request_context: OtaDscMaterializationRequest,
    monkeypatch: pytest.MonkeyPatch,
    complete: bool,
    returncode: int,
) -> None:
    mock_report(
        request_context, monkeypatch, [SYSTEM, DRIVERKIT], [DRIVERKIT], complete=complete, returncode=returncode
    )

    with pytest.raises(OtaDscProtocolError, match="complete=true with structured errors|completeness disagrees"):
        extract_ota(request_context)


def test_excluded_files_are_still_validated(
    request_context: OtaDscMaterializationRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_report(request_context, monkeypatch, [SYSTEM, DRIVERKIT], [DRIVERKIT])

    driverkit = request_context.output_root / DRIVERKIT
    driverkit.unlink()
    driverkit.symlink_to(request_context.output_root / SYSTEM)

    with pytest.raises(OtaDscProtocolError, match="duplicate path|not a regular file"):
        extract_ota(request_context)


def test_excluded_failures_do_not_bypass_requested_architecture(
    request_context: OtaDscMaterializationRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = replace(request_context, requested_arch=Arch.X86_64)
    mock_report(request, monkeypatch, [SYSTEM, DRIVERKIT], [DRIVERKIT])

    with pytest.raises(OtaDscProtocolError, match="requested architecture"):
        extract_ota(request)
