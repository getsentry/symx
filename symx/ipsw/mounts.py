"""Fail-closed cleanup for owned IPSW mount/materialization workspaces."""

import logging
import plistlib
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from symx.ipsw.errors import IpswExtractError, IpswMountCleanupError

logger = logging.getLogger(__name__)
_DETACH_TIMEOUT = 60


class _MountModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class MountEntity(_MountModel):
    mount_point: str | None = Field(None, alias="mount-point")
    device: str | None = Field(None, alias="dev-entry")


class MountedImage(_MountModel):
    image_path: str = Field(alias="image-path")
    entities: tuple[MountEntity, ...] = Field(alias="system-entities")


class MountInventory(_MountModel):
    images: tuple[MountedImage, ...]


def _inventory() -> MountInventory:
    result = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, timeout=_DETACH_TIMEOUT)
    if result.returncode != 0:
        raise IpswMountCleanupError("Cannot inspect disk images to confirm detach")

    return MountInventory.model_validate(plistlib.loads(result.stdout))


def _inside(path: str, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def _owned_images(inventory: MountInventory, root: Path) -> tuple[MountedImage, ...]:
    return tuple(
        image
        for image in inventory.images
        if _inside(image.image_path, root)
        or any(entity.mount_point and _inside(entity.mount_point, root) for entity in image.entities)
    )


def ensure_detached(root: Path) -> None:
    """Inspect by backing-file ownership too: acquisition may never report readiness.

    A killed ipsw process is not evidence of detach. Do not traverse root until
    all its disk images are absent from a fresh hdiutil inventory. Detach only
    images owned by this private root, never global/stale images by basename.
    """
    try:
        images = _owned_images(_inventory(), root)
        for image in images:
            target = next((e.device for e in image.entities if e.device), None)
            if target is None:
                target = next((e.mount_point for e in image.entities if e.mount_point), None)
            if target is None:
                raise IpswMountCleanupError(f"Cannot identify attached device for {image.image_path}")

            result = subprocess.run(["hdiutil", "detach", target], capture_output=True, timeout=_DETACH_TIMEOUT)
            logger.info("IPSW detach target=%s returncode=%s", target, result.returncode)

        if _owned_images(_inventory(), root):
            raise IpswMountCleanupError(f"IPSW image still attached; retaining workspace {root}")

        # Do not walk the tree to find mount points; that could enter a live
        # volume. Our explicit mount location is an extra fail-closed check.
        if (root / "mount").is_mount() or root.is_mount():
            raise IpswMountCleanupError(f"IPSW mount still active; retaining workspace {root}")
    except IpswMountCleanupError:
        raise
    except BaseException as error:
        # Cancellation during confirmation is not evidence of detach either.
        raise IpswMountCleanupError(f"Cannot confirm IPSW detach; retaining workspace {root}: {error}") from error

    logger.info("IPSW detach confirmed for %s", root)


@contextmanager
def image_workspace(root: Path) -> Generator[Path, None, None]:
    """A fresh directory with no implicit/finalizer deletion of mounted inputs."""
    try:
        root.mkdir(parents=True)
    except FileExistsError as error:
        # We do not own an earlier run's resources. A normal source failure
        # would allow the runner to recursively delete a possibly live mount.
        raise IpswMountCleanupError(f"Refusing to reuse existing IPSW image workspace: {root}") from error

    primary: BaseException | None = None
    try:
        yield root
    except BaseException as error:
        primary = error
        raise
    finally:
        if not isinstance(primary, IpswMountCleanupError):
            try:
                ensure_detached(root)
            except IpswMountCleanupError as cleanup_error:
                if primary is not None:
                    cleanup_error.add_note(f"Original extraction failure: {primary}")
                    raise cleanup_error from primary

                raise

            try:
                shutil.rmtree(root)
            except OSError as error:
                # Detach succeeded: this is a source failure, not a live mount
                # emergency. Keep a preceding extraction exception primary.
                if primary is not None:
                    primary.add_note(f"Detached workspace cleanup also failed: {error}")
                else:
                    raise IpswExtractError(f"Failed to remove detached workspace {root}: {error}") from error


@contextmanager
def extraction_directory() -> Generator[Path, None, None]:
    """CLI owner: unlike TemporaryDirectory, retain the tree on unsafe detach."""
    root = Path(tempfile.mkdtemp(prefix="symx_ipsw_"))
    retain = False
    try:
        yield root
    except IpswMountCleanupError:
        retain = True
        logger.critical("Unsafe IPSW cleanup: worker stopped; workspace retained at %s", root)
        raise
    finally:
        if not retain:
            shutil.rmtree(root)
