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

    # we stored the extracted dyld_shared_cache (optimization, not implemented yet)
    DSC_EXTRACTED = "dsc_extracted"

    # there was no dyld_shared_cache in the artifact (for instance: because it was a partial update)
    DSC_EXTRACTION_FAILED = "dsc_extraction_failed"

    # the artifact is a delta/patch OTA (contains image_patches/app_patches instead of full files)
    # these never contain a DSC and cannot be processed for symbols
    DELTA_OTA = "delta_ota"

    # the artifact is a recovery OS update (com.apple.MobileAsset.RecoveryOSUpdate) — a minimal
    # boot environment (kernel + small rootfs + firmware) with no DSC
    RECOVERY_OTA = "recovery_ota"

    # the symx goal: symbols are stored for symbolicator to grab
    SYMBOLS_EXTRACTED = "symbols_extracted"

    # this would typically happen when we want to update the symbol store from a given image atomically,
    # and it turns out there are debug-ids already present but with different hash or something similar.
    SYMBOL_EXTRACTION_FAILED = "symbol_extraction_failed"

    # we already know that the bundle_id is too coarse to discriminate between sensible duplicates. we probably should
    # merge rather ignore images that result in existing bundle-ids. until this is implemented we mark images with this.
    BUNDLE_DUPLICATION_DETECTED = "bundle_duplication_detected"

    # manually assigned to ignore artifact from any processing
    IGNORED = "ignored"


def github_run_id() -> int:
    return int(os.getenv("GITHUB_RUN_ID", 0))
