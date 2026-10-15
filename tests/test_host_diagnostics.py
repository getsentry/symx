import logging
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, TimeoutExpired

import pytest

from symx import host_diagnostics


@dataclass
class FakeHost:
    developer_dir: Path
    outputs: dict[tuple[str, ...], str | Exception]
    calls: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def bundle(self) -> Path:
        return self.developer_dir / "Platforms/iPhoneOS.platform/usr/lib/dsc_extractor.bundle"

    def run(self, command: list[str], **kwargs: object) -> CompletedProcess[str]:
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 15}

        key = tuple(command)
        self.calls.append(key)
        output = self.outputs[key]

        if isinstance(output, Exception):
            raise output

        return CompletedProcess(command, 0, stdout=output, stderr="")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    developer_dir = tmp_path / "Selected Xcode.app/Contents/Developer"

    fake = FakeHost(
        developer_dir,
        {
            ("/usr/bin/sw_vers",): "ProductName: macOS\nProductVersion: 27.0.1\nBuildVersion: 26A434\n",
            ("/usr/bin/xcode-select", "-p"): f"{developer_dir}\n",
            (str(developer_dir / "usr/bin/xcodebuild"), "-version"): "Xcode 27.0\nBuild version 27A266a\n",
        },
    )
    fake.bundle.parent.mkdir(parents=True)
    fake.bundle.touch()

    monkeypatch.setattr(host_diagnostics.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host_diagnostics.platform, "release", lambda: "27.0.0")
    monkeypatch.setattr(host_diagnostics.platform, "machine", lambda: "arm64")
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)
    monkeypatch.setattr(host_diagnostics.subprocess, "run", fake.run)

    return fake


@pytest.mark.parametrize("macos,xcode", [("15.7", "16.4"), ("27.0.1", "27.0"), ("28.0", "28.0 beta")])
def test_logs_actual_versions_and_selection_without_enforcing_versions(
    host: FakeHost, caplog: pytest.LogCaptureFixture, macos: str, xcode: str
) -> None:
    host.outputs[("/usr/bin/sw_vers",)] = f"ProductVersion: {macos}\nBuildVersion: OS-build"
    host.outputs[(str(host.developer_dir / "usr/bin/xcodebuild"), "-version")] = (
        f"Xcode {xcode}\nBuild version Xcode-build"
    )

    with caplog.at_level(logging.INFO):
        host_diagnostics.log_extraction_host()

    assert f"ProductVersion: {macos}" in caplog.text
    assert f"Xcode {xcode}" in caplog.text
    assert "OS-build" in caplog.text
    assert "Xcode-build" in caplog.text
    assert "Darwin 27.0.0 (arm64)" in caplog.text
    assert "DEVELOPER_DIR=<unset>" in caplog.text
    assert str(host.developer_dir) in caplog.text
    assert str(host.bundle) in caplog.text
    assert "present=True" in caplog.text
    assert host.calls == list(host.outputs)


def test_logs_developer_dir_override(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DEVELOPER_DIR", str(host.developer_dir.parent.parent))

    with caplog.at_level(logging.INFO):
        host_diagnostics.log_extraction_host()

    assert f"DEVELOPER_DIR={host.developer_dir.parent.parent}" in caplog.text


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_non_macos_logs_host_without_probing_apple_tools(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, system: str
) -> None:
    monkeypatch.setattr(host_diagnostics.platform, "system", lambda: system)

    with caplog.at_level(logging.INFO):
        host_diagnostics.log_extraction_host()

    assert system in caplog.text
    assert host.calls == []


@pytest.mark.parametrize("command_index", [0, 1, 2])
@pytest.mark.parametrize("failure", ["missing", "failed", "timeout", "encoding", "empty"])
def test_probe_failures_warn_without_stopping_extraction(
    host: FakeHost, caplog: pytest.LogCaptureFixture, command_index: int, failure: str
) -> None:
    command = list(host.outputs)[command_index]
    failures: dict[str, str | Exception] = {
        "missing": FileNotFoundError("tool missing"),
        "failed": CalledProcessError(1, command, stderr="selected developer directory is invalid"),
        "timeout": TimeoutExpired(command, 15),
        "encoding": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
        "empty": "",
    }
    host.outputs[command] = failures[failure]

    with caplog.at_level(logging.INFO):
        host_diagnostics.log_extraction_host()

    assert command[0] in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)

    if failure == "failed":
        assert "selected developer directory is invalid" in caplog.text

    # A failed OS probe must not prevent recording the independently useful Xcode details.
    if command_index == 0:
        assert "Xcode 27.0" in caplog.text
        assert str(host.bundle) in caplog.text

    if command_index == 2:
        assert str(host.bundle) in caplog.text


def test_missing_extractor_is_logged_not_rejected(host: FakeHost, caplog: pytest.LogCaptureFixture) -> None:
    host.bundle.unlink()

    with caplog.at_level(logging.INFO):
        host_diagnostics.log_extraction_host()

    assert str(host.bundle) in caplog.text
    assert "present=False" in caplog.text


def test_invalid_selection_is_logged_not_executed(host: FakeHost, caplog: pytest.LogCaptureFixture) -> None:
    host.outputs[("/usr/bin/xcode-select", "-p")] = "relative/Contents/Developer"

    host_diagnostics.log_extraction_host()

    assert "relative/Contents/Developer" in caplog.text
    assert len(host.calls) == 2
