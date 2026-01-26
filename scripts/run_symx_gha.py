#!/usr/bin/env python3
import os
import shlex
import subprocess
import sys


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

    print("Running:", " ".join(shlex.quote(a) for a in cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
