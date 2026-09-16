import plistlib
import subprocess
from pathlib import Path

import pytest

from symx.ipsw import mounts
from symx.ipsw.errors import IpswExtractError, IpswMountCleanupError


def test_cli_retains_processing_directory_on_unresolved_detach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import symx.ipsw.app as app

    roots: list[Path] = []

    def storage(root: Path, uri: str) -> object:
        roots.append(root)
        (root / "live-volume").mkdir()
        (root / "live-volume" / "do-not-delete").touch()
        return object()

    def fail(*args: object) -> None:
        raise IpswMountCleanupError("still attached")

    monkeypatch.setattr(app, "init_storage", storage)
    monkeypatch.setattr(app, "extract_runner", fail)
    with pytest.raises(IpswMountCleanupError):
        app.extract(storage="unused-local-fake", timeout=1)
    assert (roots[0] / "live-volume" / "do-not-delete").exists()
    # It is only a dummy directory; explicitly release the test's retained tree.
    import shutil

    shutil.rmtree(roots[0])


def test_existing_workspace_is_an_unsafe_ownership_conflict(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    root.mkdir()
    (root / "possibly-live-volume").mkdir()
    with pytest.raises(IpswMountCleanupError, match="existing"):
        with mounts.image_workspace(root):
            pytest.fail("must not reuse a potentially mounted workspace")
    assert (root / "possibly-live-volume").exists()


def test_unknown_mount_inventory_retains_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, b"not a plist", b"")

    monkeypatch.setattr(mounts.subprocess, "run", unreadable)
    root = tmp_path / "owned"
    with pytest.raises(IpswMountCleanupError):
        with mounts.image_workspace(root):
            (root / "backing.dmg").touch()
    assert (root / "backing.dmg").exists()


def test_detach_failure_keeps_original_exception_as_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "owned"
    info = {"images": [{"image-path": str(root / "backing.dmg"), "system-entities": [{"dev-entry": "/dev/test"}]}]}
    commands: list[list[str]] = []

    def busy(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[1] == "info":
            return subprocess.CompletedProcess(command, 0, plistlib.dumps(info), b"")
        return subprocess.CompletedProcess(command, 1, b"", b"busy")

    monkeypatch.setattr(mounts.subprocess, "run", busy)
    original = IpswExtractError("split failed first")
    with pytest.raises(IpswMountCleanupError) as caught:
        with mounts.image_workspace(root):
            (root / "backing.dmg").touch()
            raise original
    assert caught.value.__cause__ is original
    assert (root / "backing.dmg").exists()
    assert ["hdiutil", "detach", "/dev/test"] in commands


def test_file_cleanup_failure_after_detach_is_not_a_live_mount_emergency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "owned"

    def detached(*args: object, **kwargs: object):
        return subprocess.CompletedProcess([], 0, plistlib.dumps({"images": []}), b"")

    def cannot_remove(path: Path):
        raise OSError("backing deletion denied")

    monkeypatch.setattr(mounts.subprocess, "run", detached)
    monkeypatch.setattr(mounts.shutil, "rmtree", cannot_remove)
    with pytest.raises(IpswExtractError, match="Failed to remove detached workspace") as caught:
        with mounts.image_workspace(root):
            (root / "backing.dmg").touch()
    assert not isinstance(caught.value, IpswMountCleanupError)


def test_detach_confirmation_not_process_status_controls_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "owned"
    mounted = True
    unrelated = {"image-path": "/elsewhere/not-ours.dmg", "system-entities": [{"dev-entry": "/dev/other"}]}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal mounted
        if command[1] == "detach":
            assert command[-1] == "/dev/owned"
            mounted = False
            return subprocess.CompletedProcess(command, 0, b"", b"")
        images = [unrelated]
        if mounted:
            images.append({"image-path": str(root / "backing.dmg"), "system-entities": [{"dev-entry": "/dev/owned"}]})
        return subprocess.CompletedProcess(command, 0, plistlib.dumps({"images": images}), b"")

    monkeypatch.setattr(mounts.subprocess, "run", run)
    with mounts.image_workspace(root):
        (root / "backing.dmg").touch()
    assert not root.exists()
