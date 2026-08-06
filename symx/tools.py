"""Wrappers for external CLI tools: ipsw and symsorter."""

import logging
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess

import sentry_sdk

logger = logging.getLogger(__name__)

MINIMUM_IPSW_VERSION = "3.1.707"
_IPSW_RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def ipsw_version() -> str:
    result = subprocess.run(["ipsw", "version"], capture_output=True, check=True)
    output = result.stdout.decode("utf-8")
    match = re.search("Version: (.*),", output)
    if match:
        version = match.group(1)
        return version

    raise RuntimeError(f"Couldn't parse version from ipsw output: {output}")


def _parse_ipsw_release_version(version: str) -> tuple[int, int, int]:
    if _IPSW_RELEASE_VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"Unexpected ipsw version format: {version!r}")
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def validate_shell_deps() -> None:
    try:
        version = ipsw_version()
        parsed_version = _parse_ipsw_release_version(version)
    except (OSError, subprocess.CalledProcessError, RuntimeError, UnicodeDecodeError, ValueError) as error:
        logger.error("Cannot determine ipsw version: %s", error)
        sys.exit(1)

    if parsed_version < _parse_ipsw_release_version(MINIMUM_IPSW_VERSION):
        logger.error("ipsw %s is too old; version %s or newer is required", version, MINIMUM_IPSW_VERSION)
        sys.exit(1)

    logger.info("Using ipsw %s", version)
    sentry_sdk.set_tag("ipsw.version", version)

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
    output_dir: Path,
    prefix: str,
    bundle_id: str,
    split_dir: Path | Sequence[Path],
    ignore_errors: bool = False,
) -> CompletedProcess[bytes]:
    input_paths = [split_dir] if isinstance(split_dir, Path) else list(split_dir)
    with sentry_sdk.start_span(op="subprocess.symsort", name=f"Symsort {prefix}/{bundle_id}") as span:
        span.set_data("prefix", prefix)
        span.set_data("bundle_id", bundle_id)
        span.set_data("split_dirs", [str(path) for path in input_paths])

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

        symsorter_args.extend(input_paths)

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
