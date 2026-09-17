"""CI disk guard tests never fill a disk or signal unrelated host processes."""

import importlib.util
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from symx import ci_disk_guard as guard

import pytest


def load_wrapper():
    spec = importlib.util.spec_from_file_location("run_symx_gha", Path("scripts/run_symx_gha.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("lane", ["ota", "ipsw"])
def test_extraction_ci_wrapper_uses_guard(monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    wrapper = load_wrapper()
    guarded = Mock(return_value=0)
    ordinary = Mock(return_value=0)
    monkeypatch.setenv("SYMX_RUN", f"{lane} extract -t 330 -s gs://example")
    monkeypatch.setenv("SYMX_CI_DISK_GUARD", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(wrapper, "run_guarded", guarded, raising=False)
    monkeypatch.setattr(wrapper.subprocess, "call", ordinary)

    assert wrapper.main() == 0
    guarded.assert_called_once()
    ordinary.assert_not_called()


def test_reusable_macos_workflow_enables_disk_guard() -> None:
    workflow = Path(".github/workflows/symx-runner-macos.yml").read_text()
    assert 'SYMX_CI_DISK_GUARD: "1"' in workflow
    simulator = Path(".github/workflows/symx-simulator-extract.yml").read_text()
    assert "SYMX_CI_DISK_GUARD" not in simulator


@pytest.mark.parametrize("lane", ["sim extract", "ota mirror", "ipsw meta-sync"])
def test_guard_refuses_other_jobs(monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    wrapper = load_wrapper()
    monkeypatch.setenv("SYMX_RUN", lane)
    monkeypatch.setenv("SYMX_CI_DISK_GUARD", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(sys, "platform", "darwin")
    guarded = Mock()
    ordinary = Mock()
    monkeypatch.setattr(wrapper, "run_guarded", guarded)
    monkeypatch.setattr(wrapper.subprocess, "call", ordinary)
    assert wrapper.main() == 2
    guarded.assert_not_called()
    ordinary.assert_not_called()


@pytest.mark.parametrize("github_actions, platform", [("", "darwin"), ("true", "linux")])
def test_guard_refuses_non_macos_ci(monkeypatch: pytest.MonkeyPatch, github_actions: str, platform: str) -> None:
    wrapper = load_wrapper()
    monkeypatch.setenv("SYMX_RUN", "ota extract")
    monkeypatch.setenv("SYMX_CI_DISK_GUARD", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    monkeypatch.setattr(sys, "platform", platform)
    guarded = Mock()
    monkeypatch.setattr(wrapper, "run_guarded", guarded)
    assert wrapper.main() == 2
    guarded.assert_not_called()


def test_unguarded_simulator_job_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = load_wrapper()
    monkeypatch.setenv("SYMX_RUN", "sim extract -s 'bucket with spaces'")
    monkeypatch.delenv("SYMX_CI_DISK_GUARD", raising=False)
    ordinary = Mock(return_value=17)
    guarded = Mock()
    monkeypatch.setattr(wrapper.subprocess, "call", ordinary)
    monkeypatch.setattr(wrapper, "run_guarded", guarded)
    assert wrapper.main() == 17
    ordinary.assert_called_once_with([sys.executable, "-m", "symx", "sim", "extract", "-s", "bucket with spaces"])
    guarded.assert_not_called()


def disk_error(path: Path) -> guard.DiskSpaceExhaustedError:
    return guard.DiskSpaceExhaustedError(
        (guard.DiskSample(str(path), 100 * 1024**3, 100 * 1024**3 - 200 * 1024**2, 200 * 1024**2),),
        guard.MINIMUM_FREE_BYTES,
    )


def test_wrapper_reports_original_low_disk_sample_to_sentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wrapper = load_wrapper()
    error = disk_error(tmp_path)
    error.worker_pid = 43210
    guarded = Mock(side_effect=error)
    sentry = Mock()
    setup = Mock()
    monkeypatch.setenv("SYMX_RUN", "ota extract")
    monkeypatch.setenv("SYMX_CI_DISK_GUARD", "1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(wrapper, "run_guarded", guarded)
    monkeypatch.setattr(wrapper, "sentry_sdk", sentry)
    monkeypatch.setattr(wrapper, "setup_sentry", setup)

    assert wrapper.main() == 1
    setup.assert_called_once()
    sentry.capture_exception.assert_called_once_with(error)
    sentry.set_context.assert_called_once_with("ci_disk_guard", error.context())
    sentry.flush.assert_called_once_with(timeout=5)
    assert "CI runner disk exhaustion" in capsys.readouterr().err


def test_preflight_checks_both_filesystems_without_starting_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    temp = tmp_path / "temp"
    workspace.mkdir()
    temp.mkdir()
    observed: list[Path] = []

    def disk_usage(path: Path):
        observed.append(path)
        free = 20 * 1024**3 if path == workspace else 200 * 1024**2
        return SimpleNamespace(total=100 * 1024**3, used=100 * 1024**3 - free, free=free)

    popen = Mock()
    monkeypatch.setattr(guard.shutil, "disk_usage", disk_usage)
    monkeypatch.setattr(guard.subprocess, "Popen", popen)
    with pytest.raises(guard.DiskSpaceExhaustedError) as caught:
        guard.run_guarded(["worker"], paths=(workspace, temp))
    assert observed == [workspace, temp]
    assert caught.value.worker_pid is None
    assert caught.value.samples[1].free_bytes == 200 * 1024**2
    popen.assert_not_called()


@pytest.mark.parametrize("returncode", [0, 1, 137, -signal.SIGKILL])
def test_healthy_disk_preserves_worker_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int) -> None:
    process = Mock()
    process.wait.return_value = returncode
    popen = Mock(return_value=process)
    monkeypatch.setattr(guard.subprocess, "Popen", popen)
    monkeypatch.setattr(
        guard.shutil, "disk_usage", lambda path: SimpleNamespace(total=2**32, used=3 * 2**30, free=2**30)
    )
    abort = Mock()
    monkeypatch.setattr(guard, "_abort_worker", abort)
    assert guard.run_guarded(["worker"], paths=(tmp_path,)) == returncode
    popen.assert_called_once_with(["worker"], start_new_session=True)
    abort.assert_not_called()


def test_threshold_crossing_aborts_before_propagating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = disk_error(tmp_path)
    check = Mock(side_effect=[None, error])
    process = Mock(pid=43210)
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 1)
    abort = Mock()
    monkeypatch.setattr(guard, "_check_disk_space", check)
    monkeypatch.setattr(guard.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(guard, "_abort_worker", abort)
    with pytest.raises(guard.DiskSpaceExhaustedError) as caught:
        guard.run_guarded(["worker"], paths=(tmp_path,))
    assert caught.value is error
    assert error.worker_pid == 43210
    process.wait.assert_called_once_with(timeout=1.0)
    abort.assert_called_once_with(process, error)
    assert check.call_count == 2


def test_emergency_stop_includes_detached_descendants_not_unrelated_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(pid=43210)
    process.poll.return_value = None
    snapshots = Mock(
        side_effect=[
            {43210: 100, 43211: 43210, 90000: 100},
            {43210: 100, 43211: 43210, 43212: 43211, 90000: 100},
            {43210: 100, 43211: 43210, 43212: 43211, 90000: 100},
        ]
    )
    signals = Mock()
    monkeypatch.setattr(guard, "_process_parents", snapshots)
    monkeypatch.setattr(guard, "_signal", signals)
    guard._abort_worker(process, RuntimeError("disk pressure"))
    assert signals.call_args_list[0] == call(43210, signal.SIGSTOP, group=True)
    for pid in (43211, 43212):
        assert call(pid, signal.SIGSTOP) in signals.call_args_list
        assert call(pid, signal.SIGKILL) in signals.call_args_list
    assert call(43210, signal.SIGKILL, group=True) in signals.call_args_list
    assert all(args.args[0] not in (100, 90000) for args in signals.call_args_list)
    process.wait.assert_called_once_with(timeout=10)


def test_snapshot_failure_does_not_prevent_group_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock(pid=43210)
    process.poll.return_value = None
    error = RuntimeError("disk pressure")
    signals = Mock()
    monkeypatch.setattr(guard, "_process_parents", Mock(side_effect=OSError("ps failed")))
    monkeypatch.setattr(guard, "_signal", signals)
    guard._abort_worker(process, error)
    assert call(43210, signal.SIGKILL, group=True) in signals.call_args_list
    assert "ps failed" in error.__notes__[0]


def test_real_worker_and_detached_tool_stop_without_artifact_blame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real isolated processes, but only synthetic disk samples and local state.
    # The worker would persist failure if only its tool were killed.
    state = tmp_path / "artifact-state"
    state.write_text("mirrored")
    ready = tmp_path / "tool-pid"
    code = (
        "import subprocess, sys; from pathlib import Path; "
        "tool = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], start_new_session=True); "
        f"Path({str(ready)!r}).write_text(str(tool.pid)); "
        "tool.wait(); "
        f"Path({str(state)!r}).write_text('failed')"
    )

    def check(paths: tuple[Path, ...], minimum_free_bytes: int) -> None:
        if ready.exists():
            raise disk_error(tmp_path)

    monkeypatch.setattr(guard, "_check_disk_space", check)
    monkeypatch.setattr(guard, "POLL_INTERVAL_SECONDS", 0.05)
    with pytest.raises(guard.DiskSpaceExhaustedError):
        guard.run_guarded([sys.executable, "-c", code], paths=(tmp_path,))
    assert state.read_text() == "mirrored"
    tool_pid = int(ready.read_text())
    status = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(tool_pid)], capture_output=True, text=True)
    assert not status.stdout.strip() or status.stdout.strip().startswith("Z")
