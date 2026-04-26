"""Core domain types and constants shared across the symx package."""

import logging
import os
import time as time_module
from datetime import timedelta
from enum import StrEnum

logger = logging.getLogger(__name__)

HASH_BLOCK_SIZE = 2**16

MiB = 1024 * 1024


class Timeout:
    """Tracks elapsed time and checks whether a timeout has been exceeded."""

    def __init__(self, limit: timedelta) -> None:
        self._limit_seconds = limit.total_seconds()
        self._start = time_module.time()

    def exceeded(self) -> bool:
        return (time_module.time() - self._start) > self._limit_seconds

    @property
    def elapsed_seconds(self) -> int:
        return int(time_module.time() - self._start)


class Arch(StrEnum):
    ARM64E = "arm64e"
    ARM64 = "arm64"
    ARM64_32 = "arm64_32"
    ARMV7 = "armv7"
    ARMV7K = "armv7k"
    ARMV7S = "armv7s"
    X86_64 = "x86_64"


class ArtifactProcessingState(StrEnum):
    """Persisted processing states shared by the IPSW and OTA pipelines.

    Important domain nuance:
    - IPSW persists state per ``IpswSource``.
    - OTA persists state per ``OtaArtifact``.

    Most enum members are assigned by current automation. ``IGNORED`` is retained as a
    manual/operator-only state and is not assigned by the checked-in workflows.
    """

    # we retrieved metadata from apple and merged it with ours
    INDEXED = "indexed"

    # beta and normal releases are often the exact same file and don't need to be stored or processed twice
    INDEXED_DUPLICATE = "indexed_duplicate"

    # sometimes Apple releases an artifact that is faulty, but where they keep the meta-data available, or they remove
    # it, but we already indexed the artifact. Download or validation will fail in this case but this shouldn't fail the
    # mirroring workflow.
    INDEXED_INVALID = "indexed_invalid"

    # we mirrored that artifact, and it is ready for further processing
    MIRRORED = "mirrored"

    # we failed to retrieve or upload the artifact (artifacts can get unreachable)
    MIRRORING_FAILED = "mirroring_failed"

    # we have meta-data that points to the mirror, but the file at the path is missing or can't be validated
    MIRROR_CORRUPT = "mirror_corrupt"

    # the artifact is a delta/patch OTA (contains image_patches/app_patches instead of full files)
    # these never contain a DSC and cannot be processed for symbols
    DELTA_OTA = "delta_ota"

    # the artifact is a recovery OS update (com.apple.MobileAsset.RecoveryOSUpdate), a minimal
    # boot environment (kernel + small rootfs + firmware) with no DSC
    RECOVERY_OTA = "recovery_ota"

    # the artifact appears to reference a full DSC in post.bom, but the current extractor tooling
    # (payloadv2 + Apple Archive handling) cannot materialize it. This is terminal for current
    # automation, but semantically distinct from delta/recovery OTAs which inherently have no full DSC.
    UNSUPPORTED_OTA_PAYLOAD = "unsupported_ota_payload"

    # the symx goal: symbols are stored for symbolicator to grab
    SYMBOLS_EXTRACTED = "symbols_extracted"

    # this would typically happen when we want to update the symbol store from a given image atomically,
    # and it turns out there are debug-ids already present but with different hash or something similar.
    SYMBOL_EXTRACTION_FAILED = "symbol_extraction_failed"

    # manual/operator-only concept, but not currently assigned in automation
    IGNORED = "ignored"


def github_run_id() -> int:
    return int(os.getenv("GITHUB_RUN_ID", 0))
