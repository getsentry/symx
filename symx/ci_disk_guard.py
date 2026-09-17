"""Fail-stop disk supervision for disposable OTA/IPSW CI workers.

The supervisor lives outside the worker: killing a tool alone would let the
worker persist an ordinary artifact failure. Never use this for local extraction
or simulator collection. A stopped CI VM may retain mounts until teardown.
"""

import os
import shutil
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

MINIMUM_FREE_BYTES = 1024**3
POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class DiskSample:
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int


class DiskSpaceExhaustedError(RuntimeError):
    """A sampled runner filesystem crossed the emergency free-space floor."""

    def __init__(self, samples: tuple[DiskSample, ...], minimum_free_bytes: int) -> None:
        self.samples = samples
        self.minimum_free_bytes = minimum_free_bytes
        self.worker_pid: int | None = None

        critical = next(sample for sample in samples if sample.free_bytes < minimum_free_bytes)

        super().__init__(
            f"CI runner disk exhaustion: {critical.path} has {critical.free_bytes} bytes free, "
            f"below the {minimum_free_bytes}-byte safety floor; aborting worker, not failing the artifact"
        )

    def context(self) -> dict[str, object]:
        return {
            "minimum_free_bytes": self.minimum_free_bytes,
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            "worker_pid": self.worker_pid,
            "filesystems": [asdict(sample) for sample in self.samples],
        }


def _check_disk_space(paths: tuple[Path, ...], minimum_free_bytes: int) -> None:
    samples: list[DiskSample] = []

    for path in paths:
        usage = shutil.disk_usage(path)
        samples.append(DiskSample(str(path), usage.total, usage.used, usage.free))

    if any(sample.free_bytes < minimum_free_bytes for sample in samples):
        raise DiskSpaceExhaustedError(tuple(samples), minimum_free_bytes)


def _descendants(root_pid: int, parents: dict[int, int]) -> set[int]:
    owned = {root_pid}

    while True:
        found = {pid for pid, parent in parents.items() if parent in owned and pid > 1}
        expanded = owned | found

        if expanded == owned:
            return owned

        owned = expanded


def _process_parents() -> dict[int, int]:
    result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True, timeout=5)

    return {int(pid): int(parent) for line in result.stdout.splitlines() for pid, parent in [line.split()]}


def _signal(pid: int, value: signal.Signals, *, group: bool = False) -> None:
    try:
        if group:
            os.killpg(pid, value)
        else:
            os.kill(pid, value)
    except ProcessLookupError:
        pass


def _abort_worker(process: subprocess.Popen[bytes], error: BaseException) -> None:
    if process.poll() is not None:
        return

    owned = {process.pid}

    try:
        # Freeze Python and ordinary tool children before killing any of them:
        # artifact exception handlers must not turn this intervention into blame.
        _signal(process.pid, signal.SIGSTOP, group=True)
        # IPSW mount helpers create their own sessions. Freeze known descendants
        # too, resampling to include children forked during the first snapshot.
        for _ in range(5):
            found = _descendants(process.pid, _process_parents())
            new = found - owned
            owned.update(new)
            for pid in new:
                _signal(pid, signal.SIGSTOP)

            if not new:
                break
        else:
            error.add_note("Process tree did not stabilize before emergency termination")
    except (OSError, subprocess.SubprocessError, ValueError) as stop_error:
        error.add_note(f"Could not fully snapshot/freeze worker descendants: {stop_error}")
    finally:
        # Kill the worker group even if process-tree inspection failed. Do not
        # signal global tool names, unrelated mounts, or the supervisor's group.
        for pid in owned - {process.pid}:
            try:
                _signal(pid, signal.SIGKILL)
            except OSError as kill_error:
                error.add_note(f"Could not kill owned process {pid}: {kill_error}")

        _signal(process.pid, signal.SIGKILL, group=True)
        process.wait(timeout=10)


def run_guarded(command: list[str], *, paths: tuple[Path, ...]) -> int:
    """Run one isolated worker, preserving its normal exit and signal status.

    Only preflight and monitoring happen in this process. stdout/stderr remain
    inherited so existing CI diagnostic logging is unchanged. No metadata or
    artifact storage is accessed by the supervisor.
    """
    paths = tuple(dict.fromkeys(path.resolve(strict=True) for path in paths))

    if not paths:
        raise ValueError("CI disk guard requires at least one filesystem path")

    _check_disk_space(paths, MINIMUM_FREE_BYTES)
    process = subprocess.Popen(command, start_new_session=True)

    try:
        while True:
            try:
                returncode = process.wait(timeout=POLL_INTERVAL_SECONDS)
            except subprocess.TimeoutExpired:
                _check_disk_space(paths, MINIMUM_FREE_BYTES)
                continue

            # Retain a disk-specific diagnosis if the worker exits between polls
            # while its filesystem is still critically low.
            _check_disk_space(paths, MINIMUM_FREE_BYTES)
            return returncode
    except BaseException as error:
        if isinstance(error, DiskSpaceExhaustedError):
            error.worker_pid = process.pid

        try:
            _abort_worker(process, error)
        except (OSError, subprocess.SubprocessError) as abort_error:
            error.add_note(f"Emergency worker termination failed: {abort_error}")
        raise
