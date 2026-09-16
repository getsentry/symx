"""SystemOS regressions at the existing extractor/runner boundaries.

The manifest topology below is reduced from the real 26A428 BuildManifest
(SHA-256 22949db91be2ddd0009a0047ceba37336452b76af3ff915f496d842b85cce6a7).
Products, boards, members and install variants are retained; DMG/cache contents
are SYNTHETIC. These tests do not establish real-artifact cache presence.

Only external tools and compression are faked. Planning, sequencing, AEA
preflight, mount cleanup, splitting orchestration, input deletion and runner
finalization execute production code. No real mounts, downloads or GCS access.
"""

import io
import plistlib
import shutil
import subprocess
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import HttpUrl

from symx.ipsw import extract as extraction
from symx.ipsw import mounts
from symx.ipsw.extract import IpswExtractError, IpswExtractionRequest
from symx.ipsw.model import IpswArtifact, IpswPlatform, IpswReleaseStatus, IpswSource
from symx.ipsw.runners import ExtractionResult, extract
from symx.model import ArtifactProcessingState
from tests.fakes import FakeTimeout
from tests.ipsw_storage_mock import InMemoryIpswStorage

COMMON = "043-70701-646.dmg.aea"
SPECIAL = "094-72631-342.dmg.aea"
ROSETTA = "094-86031-269.dmg"
FILESYSTEM = "043-70867-635.dmg.aea"
SYSTEM = "Cryptex1,SystemOS"
VARIANTS = ("Customer Erase Install (IPSW)", "Customer Upgrade Install (IPSW)", "macOS Customer")


def identity(
    product: str = "Mac13,1",
    board: str = "j375cap",
    system: str = COMMON,
    variant: str = VARIANTS[0],
) -> dict[str, object]:
    return {
        "Ap,ProductType": product,
        "Info": {"DeviceClass": board, "Variant": variant},
        "Manifest": {
            SYSTEM: {"Info": {"Path": system}},
            "Cryptex1,RosettaOS": {"Info": {"Path": ROSETTA}},
            "OS": {"Info": {"Path": FILESYSTEM}},
        },
    }


def multi_image_identities() -> list[dict[str, object]]:
    return [
        identity(product, board, member, variant)
        for variant in VARIANTS
        for product, board, member in (
            ("Mac14,8", "j180dap", COMMON),
            ("Mac13,1", "j375cap", COMMON),
            ("Mac18,5", "j873gap", SPECIAL),
        )
    ]


def make_request(
    tmp_path: Path,
    identities: list[dict[str, object]] | None = None,
    members: tuple[str, ...] = (COMMON, SPECIAL, ROSETTA, FILESYSTEM),
    platform: IpswPlatform = IpswPlatform.MACOS,
    version: str = "27.0",
) -> IpswExtractionRequest:
    ipsw = tmp_path / "UniversalMac_27.0_26A428_Restore.ipsw"
    manifest = {
        "ProductVersion": version,
        "ProductBuildVersion": "26A428",
        "SupportedProductTypes": ["Mac13,1", "Mac14,8", "Mac18,5"],
        "BuildIdentities": multi_image_identities() if identities is None else identities,
    }
    with zipfile.ZipFile(ipsw, "w") as archive:
        archive.writestr("BuildManifest.plist", plistlib.dumps(manifest))
        for member in members:
            archive.writestr(member, b"synthetic image, not a real DMG")
    processing = tmp_path / "processing"
    processing.mkdir()
    return IpswExtractionRequest(
        platform,
        ipsw,
        processing,
        version=version,
        build="26A428",
        devices=("Mac13,1", "Mac14,8", "Mac14,8-Rack", "Mac18,5"),
    )


def option(command: list[str], *flags: str) -> str | None:
    for flag in flags:
        if flag in command:
            return command[command.index(flag) + 1]
    return None


def put(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)


class FakeProcess:
    def __init__(self, output: str = "", error: bytes = b"", returncode: int | None = 0) -> None:
        self.stdout = io.StringIO(output)
        self.error = error
        self.returncode = returncode
        self.on_exit: Callable[[], None] = lambda: None

    def __enter__(self) -> "FakeProcess":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.returncode = 0
        self.on_exit()

    def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
        return b"", self.error

    def kill(self) -> None:
        self.returncode = -9


class StuckMountProcess(FakeProcess):
    """SIGINT times out; SIGKILL exits the process but leaves the image mounted."""

    def send_signal(self, sig: int) -> None:
        pass

    def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
        if timeout is not None and self.returncode is None:
            raise subprocess.TimeoutExpired("ipsw mount sys", timeout)
        return b"mount process killed, volume still mounted", b""


@dataclass(frozen=True)
class MountCall:
    member: str
    point: Path
    backing: Path
    tmpdir: str | None


class ToolHarness:
    """Scripted 3.1.718 tool boundary, with explicit defect-isolation switches.

    Permissive selection deliberately bypasses the first ambiguity error so
    independent regressions can observe omissions later in the real pipeline.
    It is NOT a claim that upstream accepts an unselected multi-SystemOS IPSW.
    """

    def __init__(self, request: IpswExtractionRequest, monkeypatch: pytest.MonkeyPatch) -> None:
        self.request = request
        self.allow_unselected_mount = False
        self.allow_unselected_dsc = False
        self.single_image = False
        self.fail_special_split = False
        self.special_dsc_absent = False
        self.present_architectures = {COMMON: {"arm64e", "x86_64"}, SPECIAL: {"arm64e"}, ROSETTA: {"x86_64"}}
        self.stuck_mount = False
        self.live_mounts: set[Path] = set()
        self.rosetta_points: dict[Path, Path] = {}
        self.mounts: list[MountCall] = []
        self.dsc_attempts: list[tuple[str, str, Path]] = []
        self.commands: list[list[str]] = []
        self.aea_keys: list[str] = []
        self.rosetta_mounts = 0
        self.split_outputs: list[tuple[str, Path]] = []
        self.archive_paths: list[Path] = []
        self.events: list[str] = []
        self.previous_mount_clean: list[bool] = []
        self.pem_db = extraction.vendored_ipsw_pem_db_path()
        assert self.pem_db is not None
        monkeypatch.setattr(
            extraction,
            "subprocess",
            SimpleNamespace(
                Popen=self.popen,
                run=self.run,
                PIPE=subprocess.PIPE,
                STDOUT=subprocess.STDOUT,
                TimeoutExpired=subprocess.TimeoutExpired,
            ),
        )
        monkeypatch.setattr(mounts, "subprocess", extraction.subprocess)
        monkeypatch.setattr(extraction, "_kill_process_group", lambda process: process.kill())
        monkeypatch.setattr(extraction, "symsort", self.symsort)
        monkeypatch.setattr(extraction, "dyld_split", self.split)
        monkeypatch.setattr(extraction, "_compress_directory", self.compress)
        monkeypatch.setattr(extraction, "_decompress_archive", self.decompress)
        real_is_mount = Path.is_mount
        monkeypatch.setattr(Path, "is_mount", lambda path: path in self.live_mounts or real_is_mount(path))

    def selected_system(self, command: list[str], allow_unselected: bool) -> str | None:
        selector = option(command, "--device")
        if selector is None:
            return COMMON if self.single_image or allow_unselected else None
        choices = {
            "mac13,1": COMMON,
            "j375cap": COMMON,
            "mac14,8": COMMON,
            "j180dap": COMMON,
            "mac18,5": SPECIAL,
            "j873gap": SPECIAL,
        }
        return choices.get(selector.lower())

    def popen(self, command: list[str], **kwargs: object) -> FakeProcess:
        self.commands.append(command)
        assert self.request.ipsw_path.exists(), "IPSW deleted before all image consumers finished"
        assert option(command, "--pem-db") == str(self.pem_db)

        if command[:3] == ["ipsw", "mount", "sys"]:
            log = kwargs["stdout"]
            assert isinstance(log, io.TextIOBase)
            return self._mount_system(command, log, kwargs.get("env"))

        assert command[:2] == ["ipsw", "extract"], command
        return self._extract_dsc(command)

    def run(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(command)
        if "aea" in command:
            return self._aea(command)

        assert command[0] == "hdiutil", command
        if command[1] == "info":
            return self._mount_inventory(command)
        if command[1] == "attach":
            return self._attach_rosetta(command)

        assert command[1] == "detach", command
        return self._detach(command)

    def _mount_system(self, command: list[str], log: io.TextIOBase, env: object) -> FakeProcess:
        member = self.selected_system(command, self.allow_unselected_mount)
        if member is None:
            output = "failed to mount sys DMG: multiple SystemOS images found; select a target with --device\n"
            log.write(output)
            log.flush()
            return FakeProcess(output, returncode=1)

        if self.mounts:
            previous = self.mounts[-1]
            self.previous_mount_clean.append(
                not previous.point.exists()
                and not previous.backing.exists()
                and not previous.backing.with_suffix("").exists()
            )

        point_arg = option(command, "--mount-point")
        assert point_arg is not None
        point = Path(point_arg)
        tmpdir = str(env["TMPDIR"]) if isinstance(env, dict) and "TMPDIR" in env else None
        backing = Path(tmpdir) / member if tmpdir else self.request.ipsw_path.parent / "global-tmp" / member
        put(backing, "encrypted backing")
        put(backing.with_suffix(""), "decrypted backing")
        put(point / "usr/lib/libSame.dylib", f"{member}.ordinary")
        self.mounts.append(MountCall(member, point, backing, tmpdir))

        output = f"Extracted {backing}\nPress Ctrl+C to unmount '{point}'\n"
        log.write(output)
        log.flush()
        self.live_mounts.add(point)
        if self.stuck_mount:
            return StuckMountProcess(output, returncode=None)
        process = FakeProcess(output, returncode=None)
        process.on_exit = lambda: self.live_mounts.discard(point)
        return process

    def _extract_dsc(self, command: list[str]) -> FakeProcess:
        arch = option(command, "-a", "--dyld-arch") or "arm64e"
        member = self.selected_system(command, self.allow_unselected_dsc)
        if arch in ("x86_64", "x86_64h") and self.request.version == "27.0":
            member = ROSETTA  # Upstream needs only Rosetta selection for these arches.
        if member is None:
            return FakeProcess(error=b"multiple SystemOS images found; select a target with --device", returncode=1)

        root_arg = option(command, "-o", "--output")
        assert root_arg is not None
        root = Path(root_arg)
        self.dsc_attempts.append((member, arch, root))
        absent = arch not in self.present_architectures[member] or (member == SPECIAL and self.special_dsc_absent)
        if absent:
            return FakeProcess(error=b"no dyld_shared_cache files found matching the specified archs", returncode=1)
        put(root / "26A428__MacOS" / f"dyld_shared_cache_{arch}", f"{member}.{arch}")
        return FakeProcess()

    def _aea(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        if "--key" in command:
            assert option(command, "--pem-db") == str(self.pem_db)
            self.aea_keys.append(Path(command[-1]).name)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def _mount_inventory(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        points = {call.point: call.backing.with_suffix("") for call in self.mounts} | self.rosetta_points
        info = {
            "images": [
                {"image-path": str(backing), "system-entities": [{"mount-point": str(point)}]}
                for point, backing in points.items()
                if point in self.live_mounts
            ]
        }
        return subprocess.CompletedProcess(command, 0, plistlib.dumps(info), b"")

    def _attach_rosetta(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        assert self.request.ipsw_path.exists(), "IPSW deleted before Rosetta processing"
        assert Path(command[-1]).name == ROSETTA
        self.rosetta_mounts += 1
        mount_arg = option(command, "-mountpoint")
        assert mount_arg is not None
        root = Path(mount_arg)
        self.rosetta_points[root] = Path(command[-1])
        self.live_mounts.add(root)
        put(root / "System/Library/dyld/dyld_shared_cache_x86_64", "rosetta.full")
        put(root / "System/x86Support/System/Library/dyld/dyld_shared_cache_x86_64", "rosetta.x86Support")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def _detach(self, command: list[str]) -> subprocess.CompletedProcess[bytes]:
        if self.stuck_mount:
            return subprocess.CompletedProcess(command, 1, b"", b"hdiutil: detach failed - Resource busy")
        self.live_mounts.discard(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def split(self, cache: Path, output: Path) -> subprocess.CompletedProcess[bytes]:
        debug_id = cache.read_text()
        self.split_outputs.append((debug_id, output))
        if self.fail_special_split and debug_id.startswith(SPECIAL):
            return subprocess.CompletedProcess(["ipsw", "dyld", "split"], 1, b"", b"special image split failed")
        put(output / "usr/lib/libSame.dylib", debug_id)
        return subprocess.CompletedProcess(["ipsw", "dyld", "split"], 0, b"", b"")

    def symsort(
        self,
        output: Path,
        prefix: str,
        bundle: str,
        source: Path,
        ignore_errors: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        self.events.append("ordinary" if ignore_errors else "symsort")
        for binary in source.rglob("*.dylib"):
            # Stand-in for debug-ID addressing: different images have identical
            # original paths, but their distinct IDs must survive accumulation.
            debug_id = binary.read_text()
            put(output / f"{debug_id}.sym", debug_id)
        return subprocess.CompletedProcess(["symsorter"], 0, b"", b"")

    def compress(self, directory: Path) -> Path:
        archive = directory.with_name(f"{directory.name}.tar.zst")
        assert archive not in self.archive_paths, "split archive overwritten by another image"
        self.archive_paths.append(archive)
        with zipfile.ZipFile(archive, "w") as zipped:
            for file in directory.rglob("*"):
                if file.is_file():
                    zipped.write(file, file.relative_to(directory))
        shutil.rmtree(directory)
        return archive

    def decompress(self, archive: Path, target: Path) -> None:
        assert not self.request.ipsw_path.exists(), "release consumed IPSW before restoring splits"
        self.events.append("restore")
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(target)


def permissive_tools(request: IpswExtractionRequest, monkeypatch: pytest.MonkeyPatch) -> ToolHarness:
    tools = ToolHarness(request, monkeypatch)
    tools.allow_unselected_mount = tools.allow_unselected_dsc = True
    return tools


def test_both_system_images_need_validated_mount_selectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(tmp_path)
    tools = ToolHarness(request, monkeypatch)

    extraction.extract_ipsw(request)

    assert Counter(call.member for call in tools.mounts) == {COMMON: 1, SPECIAL: 1}


def test_dsc_selection_is_required_independently_of_mount_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_request(tmp_path)
    tools = ToolHarness(request, monkeypatch)
    tools.allow_unselected_mount = True  # Isolate the next failure after a mount-only fix.

    extraction.extract_ipsw(request)

    assert {(image, arch) for image, arch, _ in tools.dsc_attempts} >= {
        (COMMON, "arm64e"),
        (COMMON, "arm64e_x1"),
        (SPECIAL, "arm64e"),
        (SPECIAL, "arm64e_x1"),
    }


def test_aea_preflight_checks_each_distinct_encrypted_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(tmp_path)
    tools = permissive_tools(request, monkeypatch)

    extraction.extract_ipsw(request)

    assert Counter(tools.aea_keys) == {COMMON: 1, SPECIAL: 1}


def test_final_bundle_contains_distinct_ids_from_both_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(tmp_path)
    permissive_tools(request, monkeypatch)

    symbols = extraction.extract_ipsw(request)

    assert {file.read_text() for file in symbols.glob("*.sym")} == {
        f"{COMMON}.ordinary",
        f"{SPECIAL}.ordinary",
        f"{COMMON}.arm64e",
        f"{SPECIAL}.arm64e",
        "rosetta.full",
        "rosetta.x86Support",
    }


def test_architecture_absence_is_local_to_each_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(tmp_path)
    tools = ToolHarness(request, monkeypatch)
    tools.present_architectures[SPECIAL] = {"arm64e_x1"}
    symbols = extraction.extract_ipsw(request)
    assert (symbols / f"{COMMON}.arm64e.sym").exists()
    assert (symbols / f"{SPECIAL}.arm64e_x1.sym").exists()
    assert not (symbols / f"{COMMON}.arm64e_x1.sym").exists()
    assert not (symbols / f"{SPECIAL}.arm64e.sym").exists()


def test_materialization_does_not_discover_stale_shared_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(tmp_path)
    ToolHarness(request, monkeypatch)
    stale = request.processing_dir / "earlier-artifact" / "dyld_shared_cache_arm64e"
    put(stale, "stale-cache")
    symbols = extraction.extract_ipsw(request)
    assert stale.read_text() == "stale-cache"
    assert not (symbols / "stale-cache.sym").exists()
    assert (symbols / f"{COMMON}.arm64e.sym").exists()
    assert (symbols / f"{SPECIAL}.arm64e.sym").exists()


def test_mounts_and_materialization_have_private_locations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(tmp_path)
    tools = permissive_tools(request, monkeypatch)

    extraction.extract_ipsw(request)

    assert all(call.tmpdir is not None for call in tools.mounts), "ipsw mount still uses global TMPDIR"
    roots = [root for _, _, root in tools.dsc_attempts]
    assert all(root != request.processing_dir for root in roots), "DSC discovery still scans the shared processing dir"
    assert len(set(roots)) == len(roots), "materialization attempts reuse output roots"


def test_two_image_execution_is_deduplicated_sequential_and_image_qualified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_request(tmp_path)
    tools = permissive_tools(request, monkeypatch)

    extraction.extract_ipsw(request)

    assert Counter(call.member for call in tools.mounts) == {COMMON: 1, SPECIAL: 1}
    assert tools.previous_mount_clean == [True], "previous image must be detached and backing files removed"
    arm_outputs = [output for debug_id, output in tools.split_outputs if debug_id.endswith(".arm64e")]
    assert len(arm_outputs) == len(set(arm_outputs)) == 2
    assert all(not call.point.exists() and not call.backing.exists() for call in tools.mounts)


def test_restore_symsorts_bounded_batches_instead_of_expanding_all_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_request(tmp_path)
    tools = permissive_tools(request, monkeypatch)

    extraction.extract_ipsw(request)

    final_events = [event for event in tools.events if event != "ordinary"]
    assert final_events == [event for _ in tools.archive_paths for event in ("restore", "symsort")]


def test_shared_rosetta_full_and_x86support_are_processed_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(tmp_path)
    tools = permissive_tools(request, monkeypatch)

    symbols = extraction.extract_ipsw(request)

    assert tools.rosetta_mounts == 1
    assert Counter(debug_id for debug_id, _ in tools.split_outputs if debug_id.startswith("rosetta.")) == {
        "rosetta.full": 1,
        "rosetta.x86Support": 1,
    }
    assert (symbols / "rosetta.full.sym").exists()
    assert (symbols / "rosetta.x86Support.sym").exists()
    assert not request.ipsw_path.exists()


@pytest.mark.parametrize("platform,version", [(IpswPlatform.MACOS, "26.5"), (IpswPlatform.IOS, "18.0")])
def test_single_image_control_remains_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: IpswPlatform,
    version: str,
) -> None:
    request = make_request(tmp_path, [identity()], platform=platform, version=version)
    tools = ToolHarness(request, monkeypatch)
    tools.single_image = True

    symbols = extraction.extract_ipsw(request)

    assert (symbols / f"{COMMON}.ordinary.sym").exists()
    assert (symbols / f"{COMMON}.arm64e.sym").exists()
    assert len(tools.mounts) == 1
    assert not request.ipsw_path.exists()


class LocalExtractor:
    def validate_deps(self) -> None:
        pass  # External tools are replaced by ToolHarness.

    def extract(self, request: IpswExtractionRequest) -> ExtractionResult:
        return ExtractionResult(
            symbols_dir=extraction.extract_ipsw(request),
            prefix=extraction.map_platform_to_prefix(request.platform),
            bundle_id=extraction.generate_bundle_id(request.ipsw_path.name),
        )


def runner_storage(
    request: IpswExtractionRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[InMemoryIpswStorage, IpswArtifact]:
    storage = InMemoryIpswStorage(request.processing_dir)
    artifact = IpswArtifact(
        platform=request.platform,
        version="27.0",
        build="26A428",
        release_status=IpswReleaseStatus.RELEASE,
        sources=[
            IpswSource(
                devices=list(request.devices),
                link=HttpUrl(f"https://example.invalid/{request.ipsw_path.name}"),
                processing_state=ArtifactProcessingState.MIRRORED,
            )
        ],
    )
    storage.seed_artifact(artifact)
    monkeypatch.setattr(storage, "download_ipsw", lambda source: request.ipsw_path)
    return storage, artifact


@pytest.mark.parametrize("failure", ["split", "no_supported_dsc"])
def test_later_required_image_failure_prevents_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    request = make_request(tmp_path)
    tools = permissive_tools(request, monkeypatch)
    tools.fail_special_split = failure == "split"
    tools.special_dsc_absent = failure == "no_supported_dsc"
    storage, artifact = runner_storage(request, monkeypatch)

    extract(storage, FakeTimeout(timedelta(hours=1)), extractor=LocalExtractor())

    assert storage.uploaded_symbols == [], "runner uploaded a bundle that omitted the failing required image"
    assert artifact.sources[0].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
    assert storage.meta_updates == [artifact.key]
    assert storage.clean_local_dir_count == 1


def test_complete_two_image_bundle_is_uploaded_and_finalized_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = make_request(tmp_path)
    permissive_tools(request, monkeypatch)
    storage, artifact = runner_storage(request, monkeypatch)
    uploaded_ids: set[str] = set()
    real_upload = storage.upload_symbols

    def upload(prefix: str, bundle: str, directory: Path) -> None:
        assert storage.meta_updates == []
        assert artifact.sources[0].processing_state == ArtifactProcessingState.MIRRORED
        uploaded_ids.update(file.read_text() for file in directory.glob("*.sym"))
        real_upload(prefix, bundle, directory)

    monkeypatch.setattr(storage, "upload_symbols", upload)

    extract(storage, FakeTimeout(timedelta(hours=1)), extractor=LocalExtractor())

    assert {f"{COMMON}.arm64e", f"{SPECIAL}.arm64e"} <= uploaded_ids
    assert len(storage.uploaded_symbols) == 1
    assert storage.meta_updates == [artifact.key]
    assert artifact.sources[0].processing_state == ArtifactProcessingState.SYMBOLS_EXTRACTED


def test_unresolved_mount_stops_runner_without_traversing_mount_or_starting_next_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_request(tmp_path, [identity()])
    tools = ToolHarness(request, monkeypatch)
    tools.single_image = True
    tools.stuck_mount = True
    storage, artifact = runner_storage(request, monkeypatch)
    next_source = artifact.sources[0].model_copy(update={"file_name": "next.ipsw"})
    artifact.sources.append(next_source)

    with pytest.raises(IpswExtractError):
        extract(storage, FakeTimeout(timedelta(hours=1)), extractor=LocalExtractor())

    assert len(tools.mounts) == 1
    mount = tools.mounts[0]
    assert (mount.point / "usr/lib/libSame.dylib").exists(), "cleanup traversed a live mount"
    assert mount.backing.exists() and mount.backing.with_suffix("").exists()
    assert tools.dsc_attempts == []
    assert storage.clean_local_dir_count == 0
    assert storage.uploaded_symbols == []
    assert storage.meta_updates == [artifact.key]
    assert artifact.sources[0].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
    assert next_source.processing_state == ArtifactProcessingState.MIRRORED


@pytest.mark.parametrize("component", [{}, {"Info": {}}, {"Info": {"Path": ""}}])
def test_malformed_systemos_is_rejected_before_any_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: dict[str, object],
) -> None:
    row = identity()
    row["Manifest"] = {SYSTEM: component, "OS": {"Info": {"Path": FILESYSTEM}}}
    request = make_request(tmp_path, [row])

    def unexpected_tool(*args: object, **kwargs: object) -> None:
        pytest.fail("malformed SystemOS metadata reached subprocess work instead of failing planning")

    monkeypatch.setattr(
        extraction,
        "subprocess",
        SimpleNamespace(
            run=unexpected_tool,
            Popen=unexpected_tool,
            PIPE=subprocess.PIPE,
            STDOUT=subprocess.STDOUT,
        ),
    )

    with pytest.raises((ValueError, IpswExtractError)):
        extraction.extract_ipsw(request)
    assert request.ipsw_path.exists()


@pytest.mark.parametrize("bad_member", ["missing", "duplicate", "../escape.dmg", "/absolute.dmg"])
def test_invalid_image_members_are_rejected_before_any_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_member: str,
) -> None:
    member = COMMON if bad_member in ("missing", "duplicate") else bad_member
    members = (ROSETTA,) if bad_member == "missing" else (member, ROSETTA)
    request = make_request(tmp_path, [identity(system=member)], members=members)
    if bad_member == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            with zipfile.ZipFile(request.ipsw_path, "a") as archive:
                archive.writestr(member, b"ambiguous second image")

    def unexpected_tool(*args: object, **kwargs: object) -> None:
        pytest.fail(f"invalid image member ({bad_member}) reached subprocess work instead of failing planning")

    monkeypatch.setattr(
        extraction,
        "subprocess",
        SimpleNamespace(
            run=unexpected_tool,
            Popen=unexpected_tool,
            PIPE=subprocess.PIPE,
            STDOUT=subprocess.STDOUT,
        ),
    )

    with pytest.raises((ValueError, IpswExtractError)):
        extraction.extract_ipsw(request)
    assert request.ipsw_path.exists()
