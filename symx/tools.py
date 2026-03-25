"""Wrappers for external CLI tools: ipsw and symsorter."""

import logging
import re
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess

import sentry_sdk

logger = logging.getLogger(__name__)


def ipsw_version() -> str:
    result = subprocess.run(["ipsw", "version"], capture_output=True, check=True)
    output = result.stdout.decode("utf-8")
    match = re.search("Version: (.*),", output)
    if match:
        version = match.group(1)
        return version

    raise RuntimeError(f"Couldn't parse version from ipsw output: {output}")


def validate_shell_deps() -> None:
    version = ipsw_version()
    if version:
        logger.info("Using ipsw %s" % version)
        sentry_sdk.set_tag("ipsw.version", version)
    else:
        logger.error("ipsw not installed")
        sys.exit(1)

    result = subprocess.run(["./symsorter", "--version"], capture_output=True)
    if result.returncode == 0:
        symsorter_stdout = result.stdout.decode("utf-8")
        symsorter_version_parts = symsorter_stdout.splitlines()
        if not symsorter_version_parts:
            logger.error("Cannot parse symsorter version: %s" % symsorter_stdout)
            sys.exit(1)

        symsorter_version = symsorter_version_parts[0].split(" ").pop()
        logger.info("Using symsorter %s" % symsorter_version)
        sentry_sdk.set_tag("symsorter.version", symsorter_version)
    else:
        symsorter_stderr = result.stderr.decode("utf-8")
        logger.error("symsorter failed: %s" % symsorter_stderr)
        sys.exit(1)


def symsort(
    output_dir: Path, prefix: str, bundle_id: str, split_dir: Path, ignore_errors: bool = False
) -> CompletedProcess[bytes]:
    with sentry_sdk.start_span(op="subprocess.symsort", name=f"Symsort {prefix}/{bundle_id}") as span:
        span.set_data("prefix", prefix)
        span.set_data("bundle_id", bundle_id)
        span.set_data("split_dir", str(split_dir))

        symsorter_args = [
            "./symsorter",
            "-zz",
            "-o",
            output_dir,
            "--prefix",
            prefix,
            "--bundle-id",
            bundle_id,
        ]

        if ignore_errors:
            symsorter_args.append("--ignore-errors")

        symsorter_args.append(split_dir)

        result = subprocess.run(
            symsorter_args,
            capture_output=True,
        )
        if result.returncode != 0:
            span.set_status("internal_error")
        return result


def dyld_split(dsc: Path, output_dir: Path) -> CompletedProcess[bytes]:
    with sentry_sdk.start_span(op="subprocess.dyld_split", name=f"Dyld split {dsc.name}") as span:
        span.set_data("dsc", str(dsc))
        span.set_data("output_dir", str(output_dir))

        result = subprocess.run(
            ["ipsw", "dyld", "split", str(dsc), "--output", str(output_dir)],
            capture_output=True,
        )
        if result.returncode != 0:
            span.set_status("internal_error")
        return result
