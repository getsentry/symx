#!/usr/bin/env python3
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import sentry_sdk

from symx import setup_sentry
from symx.ci_disk_guard import DiskSpaceExhaustedError, run_guarded


def main() -> int:
    """
    Lowers the exposure to "user"-supplied executable "code":
    * we only parameterize for calls to a reusable workflow
    * that workflow input is then applied to the step env, avoiding direct shell interpolation
    * then we construct the invocation from the env here as an argument vector, not via a shell
    """
    symx_run = os.environ.get("SYMX_RUN", "")
    if not symx_run.strip():
        print("SYMX_RUN is empty", file=sys.stderr)
        return 2

    args = shlex.split(symx_run)
    cmd = [sys.executable, "-m", "symx", *args]

    print("Running:", " ".join(shlex.quote(a) for a in cmd), flush=True)
    if os.environ.get("SYMX_CI_DISK_GUARD") != "1":
        return subprocess.call(cmd)

    # This fail-stop supervisor is for disposable extraction VMs, not local
    # commands, mirror/admin jobs, or collection of preinstalled simulators.
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or sys.platform != "darwin"
        or args[:2] not in (["ota", "extract"], ["ipsw", "extract"])
    ):
        print("CI disk guard requires a macOS Actions OTA/IPSW extraction worker", file=sys.stderr)
        return 2

    try:
        return run_guarded(cmd, paths=(Path.cwd(), Path(tempfile.gettempdir())))
    except DiskSpaceExhaustedError as error:
        # The child is already stopped. Preserve the pre-intervention samples,
        # not the much healthier disk state after killing it releases swap.
        print(str(error), file=sys.stderr, flush=True)
        for note in getattr(error, "__notes__", ()):
            print(note, file=sys.stderr, flush=True)
        setup_sentry()
        sentry_sdk.set_tag("failure_reason", "disk_space_exhausted")
        sentry_sdk.set_tag("symx.pipeline", args[0])
        sentry_sdk.set_context("ci_disk_guard", error.context())
        sentry_sdk.capture_exception(error)
        sentry_sdk.flush(timeout=5)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
