"""Best-effort extraction host diagnostics, not a compatibility check."""

import logging
import os
import platform
import subprocess
from pathlib import Path

from symx.diagnostics import format_command, truncate_text

logger = logging.getLogger(__name__)
_HOST_PROBE_TIMEOUT_SECONDS = 15


def _log_host_command(command: list[str]) -> str | None:
    formatted = format_command(command)

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=_HOST_PROBE_TIMEOUT_SECONDS
        )
    except subprocess.CalledProcessError as error:
        logger.warning(
            "Host diagnostic %s failed with exit code %s: %s",
            formatted,
            error.returncode,
            truncate_text(error.stderr or error.stdout),
        )

        return None
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        logger.warning("Host diagnostic %s unavailable: %s", formatted, error)

        return None

    output = result.stdout.strip()
    if not output:
        logger.warning("Host diagnostic %s returned no output", formatted)
        return None

    logger.info("Host diagnostic %s:\n%s", formatted, truncate_text(output))

    return output


def log_extraction_host() -> None:
    """Record the host and selected Xcode without rejecting any version.

    Probe failures are warnings only. Keep raw version/build output rather than
    parsing it against an expected format or supported-version policy.
    """
    system = platform.system()
    logger.info(
        "Extraction host: %s %s (%s); DEVELOPER_DIR=%s",
        system,
        platform.release(),
        platform.machine(),
        os.environ.get("DEVELOPER_DIR", "<unset>"),
    )
    if system != "Darwin":
        return

    _log_host_command(["/usr/bin/sw_vers"])
    selected = _log_host_command(["/usr/bin/xcode-select", "-p"])
    if selected is None:
        return

    developer_dir = Path(selected)
    if not developer_dir.is_absolute():
        logger.warning("Cannot inspect selected Xcode: invalid developer directory %r", selected)
        return

    # Inspect the selected installation, not an unrelated xcodebuild on PATH.
    # xcode-select honors DEVELOPER_DIR, as does ipsw's default selection.
    _log_host_command([str(developer_dir / "usr/bin/xcodebuild"), "-version"])

    dsc_extractor = developer_dir / "Platforms/iPhoneOS.platform/usr/lib/dsc_extractor.bundle"
    try:
        present = dsc_extractor.is_file()
    except OSError as error:
        logger.warning("Cannot inspect selected Xcode DSC extractor %s: %s", dsc_extractor, error)
    else:
        logger.info("Selected Xcode DSC extractor: %s present=%s", dsc_extractor, present)
