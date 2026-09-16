import io
import zipfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from symx.ipsw import extract as extraction, mounts
from symx.ipsw.errors import IpswExtractError, IpswMountCleanupError
from symx.ipsw.image_plan import build_extraction_plan
from symx.ipsw.runners import extract
from tests.fakes import FakeTimeout
from tests.test_ipsw_systemos_images import COMMON, ToolHarness, LocalExtractor, identity, make_request, runner_storage


def test_relative_cli_paths_survive_private_subprocess_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(tmp_path, [identity()])
    tools = ToolHarness(request, monkeypatch)
    tools.single_image = True
    monkeypatch.chdir(tmp_path)
    relative = replace(request, ipsw_path=Path(request.ipsw_path.name), processing_dir=Path("processing"))
    extraction.extract_ipsw(relative)
    for command in tools.commands:
        if command[:3] == ["ipsw", "mount", "sys"]:
            assert Path(command[3]).is_absolute()
        elif command[:2] == ["ipsw", "extract"]:
            assert Path(command[2]).is_absolute()


def test_absolute_subprocess_paths_do_not_change_symlink_input_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = make_request(tmp_path, [identity()])
    master = request.ipsw_path
    link = tmp_path / "working-link.ipsw"
    link.symlink_to(master)
    linked_request = replace(request, ipsw_path=link)
    tools = ToolHarness(linked_request, monkeypatch)
    tools.single_image = True
    extraction.extract_ipsw(linked_request)
    assert master.exists(), "path normalization must not turn unlink(link) into unlink(master)"
    assert not link.is_symlink()


@pytest.mark.parametrize("alias", [COMMON.upper(), f"nested/{COMMON}"])
def test_upstream_casefold_basename_member_alias_is_rejected(tmp_path: Path, alias: str) -> None:
    request = make_request(tmp_path, [identity()])
    with zipfile.ZipFile(request.ipsw_path, "a") as archive:
        archive.writestr(alias, b"would also match ipsw's member selection")
    with pytest.raises(IpswExtractError, match="ambiguous"):
        build_extraction_plan(request)


def test_interruption_during_detach_confirmation_is_unsafe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(*args: object, **kwargs: object):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mounts.subprocess, "run", interrupt)
    root = tmp_path / "owned"
    with pytest.raises(IpswMountCleanupError):
        with mounts.image_workspace(root):
            (root / "image.dmg").touch()
    assert (root / "image.dmg").exists()


def test_failed_metadata_update_does_not_hide_unsafe_cleanup_or_original_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_request(tmp_path)
    storage, artifact = runner_storage(request, monkeypatch)
    original = IpswExtractError("original split error")
    unsafe = IpswMountCleanupError("still mounted")
    unsafe.__cause__ = original

    class FailingExtractor(LocalExtractor):
        def extract(self, request):
            raise unsafe

    def update(artifact):
        raise RuntimeError("metadata unavailable")

    monkeypatch.setattr(storage, "update_meta_item", update)
    with pytest.raises(IpswMountCleanupError) as caught:
        extract(storage, FakeTimeout(timedelta(hours=1)), extractor=FailingExtractor())
    assert caught.value.__cause__ is original
    assert any("metadata unavailable" in note for note in caught.value.__notes__)
    assert storage.clean_local_dir_count == 0


def test_continuous_mount_output_does_not_bypass_readiness_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = make_request(tmp_path)
    tools = ToolHarness(request, monkeypatch)
    real_open = Path.open

    class NeverReadyLog(io.StringIO):
        reads = 0

        def readline(self, size: int = -1) -> str:
            self.reads += 1
            assert self.reads <= 2, "continuous output bypassed the readiness deadline"
            return "still mounting...\\n"

    def open_log(path: Path, *args, **kwargs):
        if path.name == "mount.log" and not args:
            return NeverReadyLog()
        return real_open(path, *args, **kwargs)

    ticks = iter([0.0, 2000.0])
    monkeypatch.setattr(Path, "open", open_log)
    monkeypatch.setattr(extraction.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="mount sys timed out"):
        extraction._IpswExtractionRun(request)._symsort_sys_image(build_extraction_plan(request).system_images[0])
    assert not tools.live_mounts


def test_readiness_timeout_reaps_process_and_detaches_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = make_request(tmp_path)
    tools = ToolHarness(request, monkeypatch)
    original = tools.popen
    processes = []

    def never_ready(command: list[str], **kwargs: object):
        process = original(command, **kwargs)
        processes.append(process)
        log = kwargs["stdout"]
        assert isinstance(log, io.TextIOBase)
        log.seek(0)
        log.truncate()
        log.flush()
        return process

    monkeypatch.setattr(extraction.subprocess, "Popen", never_ready)
    ticks = iter([0.0, 2000.0])
    monkeypatch.setattr(extraction.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="mount sys timed out"):
        extraction._IpswExtractionRun(request)._symsort_sys_image(build_extraction_plan(request).system_images[0])
    assert not tools.live_mounts
    assert processes[0].returncode is not None
    assert not tools.mounts[0].backing.exists()
