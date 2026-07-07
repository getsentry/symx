"""
Tests for extraction utility functions.

These are pure functions or functions with minimal I/O that support
the OTA and IPSW extraction workflows.
"""

import json
import plistlib
import signal
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from symx.model import Arch
from symx.ipsw import extract as ipsw_extract
from symx.ipsw.model import IpswPlatform
from symx.ipsw.extract import (
    IpswExtractionRequest,
    IpswExtractError,
    IpswExtractTimeoutError,
    _IpswExtractionRun,
    _directory_tree_delta,
    _directory_tree_stats,
    _parse_symsorter_summary,
    find_extraction_dir,
    generate_bundle_id,
    inspect_ipsw_dmg_paths,
    inspect_ipsw_product_metadata,
    map_platform_to_prefix,
)
from subprocess import CompletedProcess

from symx.ota.model import DSCSearchResult, OtaExtractError
from symx.ota.extract import (
    DYLD_AA_INCLUDE_REGEX,
    _probe_payload_with_aa,
    _probe_unsupported_payload_format,
    extract_ota,
    find_dsc,
    parse_cryptex_patch_output,
    parse_hdiutil_mount_output,
    split_dsc,
)


# --- _map_platform_to_prefix tests ---


def test_map_platform_ipados_to_ios() -> None:
    """iPadOS and iOS share the same symbol prefix."""
    assert map_platform_to_prefix(IpswPlatform.IPADOS) == "ios"


def test_map_platform_all_lowercase() -> None:
    """All platform prefixes should be lowercase."""
    for platform in IpswPlatform:
        prefix = map_platform_to_prefix(platform)
        assert prefix == prefix.lower()


# --- generate_bundle_id tests ---


def test_generate_bundle_id_standard() -> None:
    assert generate_bundle_id("iPhone14,7_18.2_22C152_Restore.ipsw") == "ipsw_iPhone14_7_18.2_22C152_Restore"


def test_generate_bundle_id_no_commas() -> None:
    assert generate_bundle_id("UniversalMac_15.0_24A5279h_Restore.ipsw") == "ipsw_UniversalMac_15.0_24A5279h_Restore"


# --- macOS DSC architecture policy tests ---


def test_macos_dsc_architectures_include_x86_64_for_macos_27_metadata() -> None:
    assert ipsw_extract._macos_dsc_architectures("27.0") == [
        Arch.ARM64E,
        Arch.X86_64,
    ]


def test_macos_dsc_architectures_keep_x86_64_before_macos_27_metadata() -> None:
    assert ipsw_extract._macos_dsc_architectures("26.5.1") == [
        Arch.ARM64E,
        Arch.X86_64,
    ]


def test_macos_dsc_architectures_raise_for_missing_version() -> None:
    with pytest.raises(IpswExtractError, match="missing or unparseable macOS version <missing>"):
        ipsw_extract._macos_dsc_architectures(None)


def test_macos_dsc_architectures_raise_for_unparseable_version() -> None:
    with pytest.raises(IpswExtractError, match="missing or unparseable macOS version 'Sequoia'"):
        ipsw_extract._macos_dsc_architectures("Sequoia")


def test_macos_extraction_requires_version_before_running_side_effects(tmp_path: Path) -> None:
    processing_dir = tmp_path / "processing"
    processing_dir.mkdir()
    ipsw_path = tmp_path / "UniversalMac_27.0_26A5353q_Restore.ipsw"
    ipsw_path.touch()
    request = IpswExtractionRequest(IpswPlatform.MACOS, ipsw_path, processing_dir)

    with pytest.raises(IpswExtractError, match="missing or unparseable macOS version <missing>"):
        _IpswExtractionRun(request)


# --- find_extraction_dir tests ---


def test_find_extraction_dir_finds_dsc_dir() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        processing_dir = Path(tmpdir)
        (processing_dir / "split_out").mkdir()
        (processing_dir / "symbols").mkdir()
        (processing_dir / "sys_mount").mkdir()
        expected = processing_dir / "iPhone14,7_18.2_22C152"
        expected.mkdir()

        result = find_extraction_dir(processing_dir)

        assert result == expected


def test_find_extraction_dir_ignores_reserved_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        processing_dir = Path(tmpdir)
        (processing_dir / "split_out").mkdir()
        (processing_dir / "symbols").mkdir()
        (processing_dir / "sys_mount").mkdir()
        # No other directory

        result = find_extraction_dir(processing_dir)

        assert result is None


def test_find_extraction_dir_returns_first_match() -> None:
    """If multiple non-reserved dirs exist, return one of them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        processing_dir = Path(tmpdir)
        (processing_dir / "dir1").mkdir()
        (processing_dir / "dir2").mkdir()

        result = find_extraction_dir(processing_dir)

        assert result in [processing_dir / "dir1", processing_dir / "dir2"]


# --- IPSW diagnostics tests ---


def test_inspect_ipsw_product_metadata_reads_build_manifest(tmp_path: Path) -> None:
    ipsw_path = tmp_path / "test.ipsw"
    build_manifest = {
        "ProductVersion": "27.0",
        "ProductBuildVersion": "26A5353q",
        "SupportedProductTypes": ["Mac17,1", "Mac17,2"],
        "BuildIdentities": [],
    }

    with zipfile.ZipFile(ipsw_path, "w") as archive:
        archive.writestr("BuildManifest.plist", plistlib.dumps(build_manifest))

    expected_metadata = ipsw_extract.IpswProductMetadata(
        version="27.0",
        build="26A5353q",
        devices=("Mac17,1", "Mac17,2"),
    )
    assert inspect_ipsw_product_metadata(ipsw_path) == expected_metadata
    processing_dir = tmp_path / "processing"
    processing_dir.mkdir()
    assert IpswExtractionRequest.from_local_ipsw(
        IpswPlatform.MACOS, ipsw_path, processing_dir
    ) == IpswExtractionRequest(
        platform=IpswPlatform.MACOS,
        ipsw_path=ipsw_path,
        processing_dir=processing_dir,
        version=expected_metadata.version,
        build=expected_metadata.build,
        devices=expected_metadata.devices,
    )


def test_inspect_ipsw_dmg_paths_prefers_systemos_and_skips_recovery_filesystem(tmp_path: Path) -> None:
    ipsw_path = tmp_path / "test.ipsw"
    build_manifest = {
        "BuildIdentities": [
            {
                "Manifest": {
                    "Cryptex1,SystemOS": {"Info": {"Path": "system.dmg.aea"}},
                    "Cryptex1,RosettaOS": {"Info": {"Path": "rosetta.dmg"}},
                    "OS": {"Info": {"Path": "filesystem.dmg.aea"}},
                },
                "Info": {"Variant": "Customer Erase Install (IPSW)"},
            },
            {
                "Manifest": {
                    "OS": {"Info": {"Path": "recovery.dmg"}},
                },
                "Info": {"Variant": "Recovery Customer Erase Install (IPSW)"},
            },
        ]
    }

    with zipfile.ZipFile(ipsw_path, "w") as archive:
        archive.writestr("BuildManifest.plist", plistlib.dumps(build_manifest))

    assert inspect_ipsw_dmg_paths(ipsw_path) == ipsw_extract.IpswDmgPaths(
        system="system.dmg.aea",
        filesystem="filesystem.dmg.aea",
        rosetta="rosetta.dmg",
        selected="system.dmg.aea",
    )


def _make_macos_run_with_manifest(tmp_path: Path, version: str, manifest: dict[str, object]) -> _IpswExtractionRun:
    ipsw_path = tmp_path / f"UniversalMac_{version}_Test_Restore.ipsw"
    build_manifest = {
        "ProductVersion": version,
        "ProductBuildVersion": "TestBuild",
        "BuildIdentities": [
            {
                "Manifest": manifest,
                "Info": {"Variant": "Customer Erase Install (IPSW)"},
            }
        ],
    }
    with zipfile.ZipFile(ipsw_path, "w") as archive:
        archive.writestr("BuildManifest.plist", plistlib.dumps(build_manifest))

    processing_dir = tmp_path / "processing"
    processing_dir.mkdir()
    return _IpswExtractionRun(
        IpswExtractionRequest(IpswPlatform.MACOS, ipsw_path, processing_dir, version=version, build="TestBuild")
    )


def test_rosetta_dmg_path_for_x86_64_dsc_allows_legacy_systemos_fallback_before_macos_27(tmp_path: Path) -> None:
    run = _make_macos_run_with_manifest(
        tmp_path,
        "26.5",
        {"Cryptex1,SystemOS": {"Info": {"Path": "system.dmg.aea"}}},
    )

    assert run._rosetta_dmg_path_for_x86_64_dsc() is None


def test_rosetta_dmg_path_for_x86_64_dsc_ignores_rosetta_before_macos_27(tmp_path: Path) -> None:
    run = _make_macos_run_with_manifest(
        tmp_path,
        "26.5",
        {
            "Cryptex1,SystemOS": {"Info": {"Path": "system.dmg.aea"}},
            "Cryptex1,RosettaOS": {"Info": {"Path": "rosetta.dmg"}},
        },
    )

    assert run._rosetta_dmg_path_for_x86_64_dsc() is None


def test_rosetta_dmg_path_for_x86_64_dsc_requires_rosetta_for_macos_27(tmp_path: Path) -> None:
    run = _make_macos_run_with_manifest(
        tmp_path,
        "27.0",
        {"Cryptex1,SystemOS": {"Info": {"Path": "system.dmg.aea"}}},
    )

    with pytest.raises(IpswExtractError, match="macOS 27.0 x86_64 DSC requires Cryptex1,RosettaOS"):
        run._rosetta_dmg_path_for_x86_64_dsc()


def test_rosetta_dmg_path_for_x86_64_dsc_rejects_encrypted_rosetta_for_macos_27(tmp_path: Path) -> None:
    run = _make_macos_run_with_manifest(
        tmp_path,
        "27.0",
        {"Cryptex1,RosettaOS": {"Info": {"Path": "rosetta.dmg.aea"}}},
    )

    with pytest.raises(IpswExtractError, match="RosettaOS DMG, but it is AEA encrypted"):
        run._rosetta_dmg_path_for_x86_64_dsc()


def test_find_rosetta_dsc_sources_finds_full_and_x86support_caches(tmp_path: Path) -> None:
    full_dsc = tmp_path / "System/Library/dyld/dyld_shared_cache_x86_64"
    full_dsc.parent.mkdir(parents=True)
    full_dsc.touch()
    x86support_dsc = tmp_path / "System/x86Support/System/Library/dyld/dyld_shared_cache_x86_64"
    x86support_dsc.parent.mkdir(parents=True)
    x86support_dsc.touch()

    assert ipsw_extract._find_rosetta_dsc_sources(tmp_path) == [
        ipsw_extract.DscSplitSource(label="x86_64", artifact=full_dsc),
        ipsw_extract.DscSplitSource(label="x86_64_x86Support", artifact=x86support_dsc),
    ]


def test_split_rosetta_dscs_splits_full_and_x86support_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ipsw_path = tmp_path / "UniversalMac_27.0_26A5353q_Restore.ipsw"
    build_manifest = {
        "BuildIdentities": [
            {
                "Manifest": {
                    "Cryptex1,RosettaOS": {"Info": {"Path": "rosetta.dmg"}},
                },
                "Info": {"Variant": "Customer Erase Install (IPSW)"},
            }
        ]
    }
    with zipfile.ZipFile(ipsw_path, "w") as archive:
        archive.writestr("BuildManifest.plist", plistlib.dumps(build_manifest))
        archive.writestr("rosetta.dmg", b"fake")

    processing_dir = tmp_path / "processing"
    processing_dir.mkdir()
    run = _IpswExtractionRun(
        IpswExtractionRequest(IpswPlatform.MACOS, ipsw_path, processing_dir, version="27.0", build="26A5353q")
    )

    mount_root = tmp_path / "mount"
    full_dsc = mount_root / "System/Library/dyld/dyld_shared_cache_x86_64"
    full_dsc.parent.mkdir(parents=True)
    full_dsc.touch()
    x86support_dsc = mount_root / "System/x86Support/System/Library/dyld/dyld_shared_cache_x86_64"
    x86support_dsc.parent.mkdir(parents=True)
    x86support_dsc.touch()

    @contextmanager
    def fake_mount(self: _IpswExtractionRun, dmg: Path) -> Generator[Path, None, None]:
        assert self is run
        assert dmg.name == "rosetta.dmg"
        yield mount_root

    split_calls: list[tuple[Path, str, str | None, Path | None]] = []

    def fake_split(
        self: _IpswExtractionRun,
        dsc_root_file: Path,
        arch_label: str,
        split_label: str | None = None,
        cleanup_dir: Path | None = None,
    ) -> Path:
        assert self is run
        split_calls.append((dsc_root_file, arch_label, split_label, cleanup_dir))
        return processing_dir / "split_out"

    monkeypatch.setattr(_IpswExtractionRun, "_mounted_readonly_dmg", fake_mount)
    monkeypatch.setattr(_IpswExtractionRun, "_ipsw_split_dsc_file", fake_split)

    assert run._split_rosetta_dscs() == ["x86_64", "x86_64_x86Support"]
    assert split_calls == [
        (full_dsc, "x86_64", "x86_64", None),
        (x86support_dsc, "x86_64_x86Support", "x86_64_x86Support", None),
    ]


def _make_ipsw_extractor(tmp_path: Path) -> _IpswExtractionRun:
    processing_dir = tmp_path / "processing"
    processing_dir.mkdir()
    ipsw_path = tmp_path / "iPad15,7_26.4.2_23E261_Restore.ipsw"
    ipsw_path.touch()
    request = IpswExtractionRequest(IpswPlatform.IPADOS, ipsw_path, processing_dir)
    return _IpswExtractionRun(request)


def test_directory_tree_stats_counts_unique_files_without_following_links(tmp_path: Path) -> None:
    file_path = tmp_path / "file"
    file_path.write_bytes(b"abc")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "other").write_bytes(b"data")
    (tmp_path / "hardlink").hardlink_to(file_path)
    (tmp_path / "symlink").symlink_to(file_path)

    stats = _directory_tree_stats(tmp_path)

    assert stats.file_count == 2
    assert stats.total_file_size_bytes == 7
    assert stats.directory_count == 1
    assert stats.symlink_count == 1


def test_directory_tree_delta_uses_zero_for_missing_before_counts(tmp_path: Path) -> None:
    before = _directory_tree_stats(tmp_path / "missing")
    (tmp_path / "created").write_bytes(b"abc")
    after = _directory_tree_stats(tmp_path)

    delta = _directory_tree_delta(before, after)
    assert delta.file_count_delta == 1
    assert delta.total_file_size_bytes_delta == 3


def test_parse_symsorter_summary_extracts_counts() -> None:
    assert _parse_symsorter_summary(
        b"Done.\nSorted 42 debug files\nCreated 2 source bundles\n",
        b"WARNING: File foo already exists, you seem to have duplicate debug files\n",
    ) == {
        "sorted_debug_files": 42,
        "created_source_bundles": 2,
        "duplicate_debug_file_warnings": 1,
    }


def test_ipsw_extract_dsc_timeout_preserves_timeout_contract_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)

    class FakePopen:
        def __init__(self, command: list[str], stdout: object = None, stderr: object = None) -> None:
            self.command = command
            self.returncode = 0

        def __enter__(self) -> "FakePopen":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.command, timeout)
            return b"timeout stdout", b"timeout stderr"

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("symx.ipsw.extract.subprocess.Popen", FakePopen)

    with pytest.raises(TimeoutError, match="ipsw extract timed out") as exc_info:
        extractor._ipsw_extract_dsc()

    assert isinstance(exc_info.value, IpswExtractTimeoutError)
    assert isinstance(exc_info.value, IpswExtractError)
    message = str(exc_info.value)
    assert message == f"ipsw extract timed out for {extractor.ipsw_path} (default)"


def test_ipsw_extract_dsc_raises_detailed_error_when_extract_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)

    class FakePopen:
        def __init__(self, command: list[str], stdout: object = None, stderr: object = None) -> None:
            self.command = command
            self.returncode = 1

        def __enter__(self) -> "FakePopen":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
            return b"extract stdout", b"extract stderr"

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("symx.ipsw.extract.subprocess.Popen", FakePopen)

    with pytest.raises(IpswExtractError, match="ipsw extract failed") as exc_info:
        extractor._ipsw_extract_dsc()

    message = str(exc_info.value)
    assert message == f"ipsw extract failed for {extractor.ipsw_path} (default) with exit code 1"


def test_ipsw_extract_dsc_passes_vendored_pem_db_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)
    pem_db = tmp_path / "fcs-keys.json"
    pem_db.write_text("{}")
    expected_extract_dir = extractor.processing_dir / "23E261__iPad15,7"
    expected_extract_dir.mkdir()
    commands: list[list[str]] = []

    class FakePopen:
        def __init__(self, command: list[str], stdout: object = None, stderr: object = None) -> None:
            commands.append(command)
            self.command = command
            self.returncode = 0

        def __enter__(self) -> "FakePopen":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
            return b"extract stdout", b"extract stderr"

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("symx.ipsw.extract.vendored_ipsw_pem_db_path", lambda: pem_db)
    monkeypatch.setattr("symx.ipsw.extract.subprocess.Popen", FakePopen)

    assert extractor._ipsw_extract_dsc() == expected_extract_dir
    assert commands == [
        [
            "ipsw",
            "extract",
            str(extractor.ipsw_path),
            "-d",
            "-o",
            str(extractor.processing_dir),
            "-V",
            "--pem-db",
            str(pem_db),
        ]
    ]


def test_ipsw_symsort_sys_image_passes_vendored_pem_db_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)
    pem_db = tmp_path / "fcs-keys.json"
    pem_db.write_text("{}")
    mount_point = extractor._sys_mount_point()
    mount_point.mkdir()
    commands: list[list[str]] = []
    communicate_timeouts: list[int | None] = []
    symsort_calls: list[tuple[Path, bool]] = []

    class FakeStdout:
        def __init__(self, lines: list[str]) -> None:
            self._lines = iter(lines)

        def readline(self) -> str:
            return next(self._lines, "")

    class FakePopen:
        def __init__(
            self,
            command: list[str],
            stdout: object = None,
            stderr: object = None,
            bufsize: int | None = None,
            text: bool | None = None,
        ) -> None:
            commands.append(command)
            self.stdout = FakeStdout([f"Press Ctrl+C to unmount '{mount_point}'\n", ""])
            self.returncode = 0

        def poll(self) -> int | None:
            return None

        def send_signal(self, sig: int) -> None:
            return None

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            communicate_timeouts.append(timeout)
            self.returncode = 0
            return "", ""

        def wait(self, timeout: int | None = None) -> int:
            raise AssertionError("mount cleanup should drain with communicate(), not wait()")

        def kill(self) -> None:
            return None

    monkeypatch.setattr("symx.ipsw.extract.vendored_ipsw_pem_db_path", lambda: pem_db)
    monkeypatch.setattr("symx.ipsw.extract.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        _IpswExtractionRun,
        "_symsort",
        lambda self, split_dir, ignore_errors=False, record_input_tree=True: symsort_calls.append(
            (split_dir, ignore_errors)
        ),
    )

    extractor._symsort_sys_image()

    assert commands == [
        [
            "ipsw",
            "mount",
            "sys",
            str(extractor.ipsw_path),
            "-V",
            "--mount-point",
            str(mount_point),
            "--pem-db",
            str(pem_db),
        ]
    ]
    assert communicate_timeouts == [60]
    assert symsort_calls == [(mount_point, True)]


def test_ipsw_symsort_sys_image_kills_mount_process_after_cleanup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)
    mount_point = extractor._sys_mount_point()
    mount_point.mkdir()
    communicate_timeouts: list[int | None] = []
    sent_signals: list[int] = []
    killed = False

    class FakeStdout:
        def __init__(self, lines: list[str]) -> None:
            self._lines = iter(lines)

        def readline(self) -> str:
            return next(self._lines, "")

    class FakePopen:
        def __init__(
            self,
            command: list[str],
            stdout: object = None,
            stderr: object = None,
            bufsize: int | None = None,
            text: bool | None = None,
        ) -> None:
            self.command = command
            self.stdout = FakeStdout([f"Press Ctrl+C to unmount '{mount_point}'\n", ""])
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def send_signal(self, sig: int) -> None:
            sent_signals.append(sig)

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            communicate_timeouts.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.command, timeout, output="partial unmount output")
            self.returncode = -9
            return "post-kill unmount output", ""

        def wait(self, timeout: int | None = None) -> int:
            raise AssertionError("mount cleanup should drain with communicate(), not wait()")

        def kill(self) -> None:
            nonlocal killed
            killed = True
            self.returncode = -9

    monkeypatch.setattr("symx.ipsw.extract.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        _IpswExtractionRun, "_symsort", lambda self, split_dir, ignore_errors=False, record_input_tree=True: None
    )

    extractor._symsort_sys_image()

    assert sent_signals == [signal.SIGINT]
    assert communicate_timeouts == [60, None]
    assert killed


def test_ipsw_symsort_sys_image_cleans_extracted_aea_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)
    mount_point = extractor._sys_mount_point()
    mount_point.mkdir()
    extracted_aea = tmp_path / "tmp" / "043-01053-377.dmg.aea"
    extracted_aea.parent.mkdir()
    extracted_aea.write_bytes(b"aea")

    class FakeStdout:
        def __init__(self, lines: list[str]) -> None:
            self._lines = iter(lines)

        def readline(self) -> str:
            return next(self._lines, "")

    class FakePopen:
        def __init__(
            self,
            command: list[str],
            stdout: object = None,
            stderr: object = None,
            bufsize: int | None = None,
            text: bool | None = None,
        ) -> None:
            self.stdout = FakeStdout(
                [
                    f"Extracted {extracted_aea} from {extractor.ipsw_path}\n",
                    f"Press Ctrl+C to unmount '{mount_point}'\n",
                    "",
                ]
            )
            self.returncode = 0

        def poll(self) -> int | None:
            return None

        def send_signal(self, sig: int) -> None:
            return None

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            self.returncode = 0
            return "", ""

        def wait(self, timeout: int | None = None) -> int:
            raise AssertionError("mount cleanup should drain with communicate(), not wait()")

        def kill(self) -> None:
            return None

    def fake_symsort(
        self: _IpswExtractionRun, split_dir: Path, ignore_errors: bool = False, record_input_tree: bool = True
    ) -> None:
        raise IpswExtractError("boom")

    monkeypatch.setattr("symx.ipsw.extract.subprocess.Popen", FakePopen)
    monkeypatch.setattr(_IpswExtractionRun, "_symsort", fake_symsort)

    with pytest.raises(IpswExtractError, match="boom"):
        extractor._symsort_sys_image()

    assert not mount_point.exists()
    assert not extracted_aea.exists()


def test_ipsw_symsort_sys_image_raises_on_mount_failure_and_cleans_mount_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)
    mount_point = extractor._sys_mount_point()
    mount_point.mkdir()
    extracted_aea = tmp_path / "tmp" / "043-01053-377.dmg.aea"
    extracted_aea.parent.mkdir()
    extracted_aea.write_bytes(b"aea")
    decrypted_dmg = extracted_aea.with_suffix("")
    decrypted_dmg.write_bytes(b"dmg")

    class FakeStdout:
        def __init__(self, lines: list[str]) -> None:
            self._lines = iter(lines)

        def readline(self) -> str:
            return next(self._lines, "")

    class FakePopen:
        def __init__(
            self,
            command: list[str],
            stdout: object = None,
            stderr: object = None,
            bufsize: int | None = None,
            text: bool | None = None,
        ) -> None:
            self.stdout = FakeStdout(
                [
                    f"Extracted {extracted_aea} from {extractor.ipsw_path}\n",
                    (
                        "failed to mount sys DMG: failed to mount "
                        f"{decrypted_dmg}: exit status 1: hdiutil: attach failed - Permission denied\n"
                    ),
                    "",
                ]
            )
            self.returncode = 1

        def poll(self) -> int | None:
            return self.returncode

        def send_signal(self, sig: int) -> None:
            return None

        def wait(self, timeout: int | None = None) -> int:
            raise AssertionError("mount cleanup should drain with communicate(), not wait()")

        def kill(self) -> None:
            return None

    monkeypatch.setattr("symx.ipsw.extract.subprocess.Popen", FakePopen)

    with pytest.raises(IpswExtractError, match="failed to mount sys DMG"):
        extractor._symsort_sys_image()

    assert not mount_point.exists()
    assert not extracted_aea.exists()
    assert not decrypted_dmg.exists()


def _make_ipsw_aea_preflight_extractor(tmp_path: Path) -> _IpswExtractionRun:
    processing_dir = tmp_path / "processing"
    processing_dir.mkdir()
    ipsw_path = tmp_path / "test.ipsw"
    build_manifest = {
        "BuildIdentities": [
            {
                "Manifest": {
                    "Cryptex1,SystemOS": {"Info": {"Path": "system.dmg.aea"}},
                    "OS": {"Info": {"Path": "filesystem.dmg.aea"}},
                },
                "Info": {"Variant": "Customer Erase Install (IPSW)"},
            }
        ]
    }

    with zipfile.ZipFile(ipsw_path, "w") as archive:
        archive.writestr("BuildManifest.plist", plistlib.dumps(build_manifest))
        archive.writestr("system.dmg.aea", b"dummy")

    request = IpswExtractionRequest(IpswPlatform.IPADOS, ipsw_path, processing_dir)
    return _IpswExtractionRun(request)


def _install_fake_aea_preflight_run(
    monkeypatch: pytest.MonkeyPatch,
    key_attempt_results: list[tuple[int, bytes, bytes]],
) -> Callable[[], int]:
    key_attempts = 0

    def fake_run(command: list[str], capture_output: bool = False) -> CompletedProcess[bytes]:
        nonlocal key_attempts
        if "--info" in command:
            return CompletedProcess(
                args=command,
                returncode=0,
                stdout=(
                    "[com.apple.wkms.fcs-key-url]:\nhttps://wkms-public.apple.com/fcs-keys/missing-key=\n"
                ).encode(),
                stderr=b"",
            )

        key_attempts += 1
        returncode, stdout, stderr = key_attempt_results[key_attempts - 1]
        return CompletedProcess(args=command, returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("symx.ipsw.extract.subprocess.run", fake_run)
    return lambda: key_attempts


def test_ipsw_aea_preflight_classifies_missing_key_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _make_ipsw_aea_preflight_extractor(tmp_path)
    pem_db = tmp_path / "fcs-keys.json"
    pem_db.write_text(json.dumps({"known-key": "known-value"}))
    get_key_attempts = _install_fake_aea_preflight_run(
        monkeypatch,
        [
            (
                1,
                b"",
                b"\xe2\xa8\xaf failed to HPKE decrypt fcs-key: failed to connect to fcs-key URL: 403 Forbidden\n",
            )
        ],
    )

    monkeypatch.setattr("symx.ipsw.extract.vendored_ipsw_pem_db_path", lambda: pem_db)
    monkeypatch.setattr("symx.ipsw.extract._vendored_ipsw_pem_db_keys", lambda: frozenset({"known-key"}))

    with pytest.raises(IpswExtractError, match="IPSW AEA preflight failed") as exc_info:
        extractor._ipsw_aea_preflight()

    message = str(exc_info.value)
    assert get_key_attempts() == 1
    assert "selected_dmg=system.dmg.aea" in message
    assert "system_dmg=system.dmg.aea" in message
    assert "filesystem_dmg=filesystem.dmg.aea" in message
    assert "fcs_key=missing-key=" in message
    assert "vendored_db_hit=False" in message
    assert "failed to HPKE decrypt fcs-key: failed to connect to fcs-key URL: 403 Forbidden" in message


def test_ipsw_aea_preflight_retries_transient_fcs_key_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _make_ipsw_aea_preflight_extractor(tmp_path)
    get_key_attempts = _install_fake_aea_preflight_run(
        monkeypatch,
        [
            (
                1,
                b"",
                (
                    b"\xe2\xa8\xaf failed to HPKE decrypt fcs-key: failed to connect to fcs-key URL: "
                    b'Get "https://wkms-public.apple.com/fcs-keys/missing-key=": '
                    b"dial tcp: lookup wkms-public.apple.com: no such host\n"
                ),
            ),
            (0, b"key", b""),
        ],
    )

    monkeypatch.setattr("symx.ipsw.extract.vendored_ipsw_pem_db_path", lambda: None)
    monkeypatch.setattr("symx.ipsw.extract._vendored_ipsw_pem_db_keys", lambda: frozenset())
    monkeypatch.setattr("symx.ipsw.extract.time.sleep", lambda seconds: None)

    extractor._ipsw_aea_preflight()

    assert get_key_attempts() == 2


def test_ipsw_extract_dsc_includes_stderr_summary_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)

    class FakePopen:
        def __init__(self, command: list[str], stdout: object = None, stderr: object = None) -> None:
            self.command = command
            self.returncode = 1

        def __enter__(self) -> "FakePopen":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
            return (
                b"extract stdout",
                b"Usage:\n  ipsw extract <IPSW/OTA | URL> [flags]\n\nError: failed to mount DMG /tmp/test.dmg",
            )

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("symx.ipsw.extract.subprocess.Popen", FakePopen)

    with pytest.raises(IpswExtractError, match="failed to mount DMG /tmp/test.dmg") as exc_info:
        extractor._ipsw_extract_dsc()

    message = str(exc_info.value)
    assert message == (
        f"ipsw extract failed for {extractor.ipsw_path} (default) with exit code 1: "
        "Error: failed to mount DMG /tmp/test.dmg"
    )


def test_ipsw_split_raises_detailed_error_when_split_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _make_ipsw_extractor(tmp_path)
    extract_dir = extractor.processing_dir / "23E261__iPad15,7"
    extract_dir.mkdir()
    (extract_dir / "dyld_shared_cache_arm64e").touch()

    def fake_dyld_split(dsc: Path, output_dir: Path) -> CompletedProcess[bytes]:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "partial").touch()
        return CompletedProcess(args=[], returncode=1, stdout=b"split stdout", stderr=b"split stderr")

    monkeypatch.setattr("symx.ipsw.extract.dyld_split", fake_dyld_split)

    with pytest.raises(IpswExtractError, match="ipsw dyld split failed") as exc_info:
        extractor._ipsw_split(extract_dir)

    message = str(exc_info.value)
    assert message == f"ipsw dyld split failed for {extract_dir / 'dyld_shared_cache_arm64e'}"


def test_ipsw_symsort_raises_detailed_error_when_symsorter_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _make_ipsw_extractor(tmp_path)
    split_dir = extractor.processing_dir / "split_out"
    split_dir.mkdir()
    (split_dir / "binary").touch()

    def fake_symsort(
        output_dir: Path, prefix: str, bundle_id: str, split_dir: Path, ignore_errors: bool = False
    ) -> CompletedProcess[bytes]:
        return CompletedProcess(args=[], returncode=1, stdout=b"symsort stdout", stderr=b"symsort stderr")

    monkeypatch.setattr("symx.ipsw.extract.symsort", fake_symsort)

    with pytest.raises(IpswExtractError, match="Symsorter failed for bundle") as exc_info:
        extractor._symsort(split_dir)

    message = str(exc_info.value)
    assert message == f"Symsorter failed for bundle {extractor.bundle_id}"


# --- parse_cryptex_patch_output tests ---


def test_parse_cryptex_patch_output_extracts_dmg_mappings() -> None:
    stderr = """• Patching cryptex-system-arm64e to /tmp/out/cryptex-system-arm64e.dmg
• Patching cryptex-app-arm64e to /tmp/out/cryptex-app-arm64e.dmg"""

    result = parse_cryptex_patch_output(stderr)

    assert result["cryptex-system-arm64e"] == Path("/tmp/out/cryptex-system-arm64e.dmg")
    assert result["cryptex-app-arm64e"] == Path("/tmp/out/cryptex-app-arm64e.dmg")


def test_parse_cryptex_patch_output_empty_on_no_match() -> None:
    assert parse_cryptex_patch_output("some other output") == {}
    assert parse_cryptex_patch_output("") == {}


# --- parse_hdiutil_mount_output tests ---


def test_parse_hdiutil_mount_output_standard() -> None:
    output = """/dev/disk4          	GUID_partition_scheme
/dev/disk4s1        	Apple_APFS
/dev/disk5          	EF57347C-0000-11AA-AA11-0030654
/dev/disk5s1        	41504653-0000-11AA-AA11-0030654	/Volumes/Macintosh HD"""

    result = parse_hdiutil_mount_output(output)

    assert result.dev == "/dev/disk5s1"
    assert result.id == "41504653-0000-11AA-AA11-0030654"
    assert result.point == Path("/Volumes/Macintosh HD")


def test_parse_hdiutil_mount_output_simple() -> None:
    output = "/dev/disk2s1\tGUID\t/Volumes/Test"
    result = parse_hdiutil_mount_output(output)

    assert result.dev == "/dev/disk2s1"
    assert result.point == Path("/Volumes/Test")


# --- split_dsc tests ---


def make_splitter(return_codes: list[int]):
    """Create a mock splitter that returns specified return codes in sequence."""
    codes = iter(return_codes)

    def splitter(dsc: Path, output_dir: Path) -> CompletedProcess[bytes]:
        return CompletedProcess(args=[], returncode=next(codes), stdout=b"", stderr=b"")

    return splitter


def test_split_dsc_all_succeed() -> None:
    search_results = [
        DSCSearchResult(arch=Arch.ARM64E, artifact=Path("/dsc1"), split_dir=Path("/out1")),
        DSCSearchResult(arch=Arch.ARM64, artifact=Path("/dsc2"), split_dir=Path("/out2")),
    ]

    result = split_dsc(search_results, splitter=make_splitter([0, 0]))

    assert result == [Path("/out1"), Path("/out2")]


def test_split_dsc_partial_failure() -> None:
    """If some splits fail, return only successful ones."""
    search_results = [
        DSCSearchResult(arch=Arch.ARM64E, artifact=Path("/dsc1"), split_dir=Path("/out1")),
        DSCSearchResult(arch=Arch.ARM64, artifact=Path("/dsc2"), split_dir=Path("/out2")),
    ]

    result = split_dsc(search_results, splitter=make_splitter([1, 0]))  # First fails

    assert result == [Path("/out2")]


def test_split_dsc_all_fail_raises() -> None:
    """If all splits fail, raise OtaExtractError."""
    search_results = [
        DSCSearchResult(arch=Arch.ARM64E, artifact=Path("/dsc1"), split_dir=Path("/out1")),
        DSCSearchResult(arch=Arch.ARM64, artifact=Path("/dsc2"), split_dir=Path("/out2")),
    ]

    with pytest.raises(OtaExtractError, match="Split failed for all"):
        split_dsc(search_results, splitter=make_splitter([1, 1]))


def test_split_dsc_empty_input_raises() -> None:
    """Empty input should raise since no splits succeed."""
    with pytest.raises(OtaExtractError):
        split_dsc([], splitter=make_splitter([]))


# --- find_dsc tests ---


def test_find_dsc_standard_path() -> None:
    """Find DSC in System/Library/dyld/ location."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"

        # Create DSC file in standard location
        dsc_dir = input_dir / "System/Library/dyld"
        dsc_dir.mkdir(parents=True)
        (dsc_dir / "dyld_shared_cache_arm64e").touch()

        results = find_dsc(input_dir, "17.0", "21A100", output_dir)

        assert len(results) == 1
        assert results[0].arch == Arch.ARM64E
        assert results[0].artifact == dsc_dir / "dyld_shared_cache_arm64e"


def test_find_dsc_cache_path() -> None:
    """Find DSC in com.apple.dyld cache location."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"

        dsc_dir = input_dir / "System/Library/Caches/com.apple.dyld"
        dsc_dir.mkdir(parents=True)
        (dsc_dir / "dyld_shared_cache_arm64").touch()

        results = find_dsc(input_dir, "17.0", "21A100", output_dir)

        assert len(results) == 1
        assert results[0].arch == Arch.ARM64


def test_find_dsc_multiple_architectures() -> None:
    """Find multiple DSC files for different architectures."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"

        dsc_dir = input_dir / "System/Library/dyld"
        dsc_dir.mkdir(parents=True)
        (dsc_dir / "dyld_shared_cache_arm64e").touch()
        (dsc_dir / "dyld_shared_cache_arm64").touch()

        results = find_dsc(input_dir, "17.0", "21A100", output_dir)

        assert len(results) == 2
        arches = {r.arch for r in results}
        assert arches == {Arch.ARM64E, Arch.ARM64}


def test_find_dsc_no_dsc_raises_error() -> None:
    """Should raise OtaExtractError if no DSC found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        input_dir.mkdir()
        output_dir = Path(tmpdir) / "output"

        with pytest.raises(OtaExtractError, match="Couldn't find any dyld_shared_cache"):
            find_dsc(input_dir, "17.0", "21A100", output_dir)


def test_find_dsc_error_includes_sample_for_unsupported_dsc_layout() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"
        driverkit_dsc_dir = input_dir / "System/DriverKit/System/Library/dyld"
        driverkit_dsc_dir.mkdir(parents=True)
        (driverkit_dsc_dir / "dyld_shared_cache_arm64e").touch()

        with pytest.raises(OtaExtractError, match="outside supported roots") as exc_info:
            find_dsc(input_dir, "17.0", "21A100", output_dir)

        assert "System/DriverKit/System/Library/dyld/dyld_shared_cache_arm64e" in str(exc_info.value)


def test_find_dsc_generates_unique_split_dirs() -> None:
    """Split dirs should be unique even if same arch found in multiple locations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"

        # Create same arch in two different locations
        loc1 = input_dir / "System/Library/dyld"
        loc1.mkdir(parents=True)
        (loc1 / "dyld_shared_cache_arm64e").touch()

        loc2 = input_dir / "System/Library/Caches/com.apple.dyld"
        loc2.mkdir(parents=True)
        (loc2 / "dyld_shared_cache_arm64e").touch()

        results = find_dsc(input_dir, "17.0", "21A100", output_dir)

        # Should find both, with unique split_dirs
        assert len(results) == 2
        split_dirs = [r.split_dir for r in results]
        assert len(set(split_dirs)) == 2  # All unique


# --- extract_ota tests ---


def test_extract_ota_raises_detailed_error_when_no_dsc_extracted(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "output"
        image_dir = output_dir / "21A100__Device1,1"

        def fake_run(args: list[str], capture_output: bool) -> CompletedProcess[bytes]:
            image_dir.mkdir(parents=True, exist_ok=True)
            if "-p" in args:
                return CompletedProcess(args=args, returncode=0, stdout=b"pattern stdout", stderr=b"pattern stderr")
            return CompletedProcess(args=args, returncode=1, stdout=b"literal stdout", stderr=b"literal stderr")

        monkeypatch.setattr("symx.ota.extract.subprocess.run", fake_run)

        with pytest.raises(OtaExtractError, match="OTA extraction produced no dyld_shared_cache files") as exc_info:
            extract_ota(Path("/tmp/test.ota"), output_dir)

        message = str(exc_info.value)
        assert message == "OTA extraction produced no dyld_shared_cache files for /tmp/test.ota"


def test_extract_ota_fails_when_payloadv2_pattern_extract_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "output"
        image_dir = output_dir / "21A100__Device1,1"

        def fake_run(args: list[str], capture_output: bool) -> CompletedProcess[bytes]:
            image_dir.mkdir(parents=True, exist_ok=True)
            if "-p" in args:
                dsc_dir = image_dir / "System/Library/Caches/com.apple.dyld"
                dsc_dir.mkdir(parents=True, exist_ok=True)
                (dsc_dir / "dyld_shared_cache_arm64e.01").touch()
                return CompletedProcess(
                    args=args,
                    returncode=2,
                    stdout=b"pattern stdout",
                    stderr=b"\xe2\xa8\xaf failed to extract payload.039\n",
                )
            return CompletedProcess(args=args, returncode=1, stdout=b"literal stdout", stderr=b"literal stderr")

        monkeypatch.setattr("symx.ota.extract.subprocess.run", fake_run)

        with pytest.raises(OtaExtractError, match="payloadv2 pattern DSC extraction failed") as exc_info:
            extract_ota(Path("/tmp/test.ota"), output_dir)

        message = str(exc_info.value)
        assert "with exit code 2" in message
        assert "failed to extract payload.039" in message


def test_extract_ota_falls_back_to_pattern_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "output"
        image_dir = output_dir / "21A100__Device1,1"

        def fake_run(args: list[str], capture_output: bool) -> CompletedProcess[bytes]:
            image_dir.mkdir(parents=True, exist_ok=True)
            if "-p" in args:
                dsc_dir = image_dir / "System/Library/Caches/com.apple.dyld"
                dsc_dir.mkdir(parents=True, exist_ok=True)
                (dsc_dir / "dyld_shared_cache_arm64e").touch()
                return CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")
            return CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"literal failed")

        monkeypatch.setattr("symx.ota.extract.subprocess.run", fake_run)

        assert extract_ota(Path("/tmp/test.ota"), output_dir) == image_dir


def test_probe_payload_with_aa_materializes_zip_member_for_subprocess_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "test.zip"
    payload_name = "AssetData/payloadv2/payload.000"
    payload_bytes = b"payloadv2 bytes"

    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(payload_name, payload_bytes)

    def fake_run(args: list[str], stdin: object | None = None, capture_output: bool = False) -> CompletedProcess[bytes]:
        assert stdin is not None
        stdin_file = cast(BinaryIO, stdin)
        stdin_file.fileno()
        assert stdin_file.read() == payload_bytes
        return CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"Invalid/non-supported archive stream")

    monkeypatch.setattr("symx.ota.extract.subprocess.run", fake_run)

    result = _probe_payload_with_aa(artifact, payload_name, DYLD_AA_INCLUDE_REGEX)

    assert result["payload"] == payload_name
    assert result["returncode"] == 1
    assert result["extracted_count"] == 0
    assert result["unsupported_error"] is True


def test_probe_unsupported_payload_format_returns_true_for_consistent_aa_legacy_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = Path("/tmp/test.zip")

    monkeypatch.setattr("symx.ota.extract._ota_is_zip_archive", lambda artifact: True)
    monkeypatch.setattr(
        "symx.ota.extract._read_post_bom_dsc_matches",
        lambda artifact: ["System/Library/Caches/com.apple.dyld/dyld_shared_cache_armv7k"],
    )
    monkeypatch.setattr(
        "symx.ota.extract._payload_entry_names",
        lambda artifact: ["AssetData/payloadv2/payload.000", "AssetData/payloadv2/payload.001"],
    )

    def fake_probe_payload_with_aa(artifact: Path, payload_name: str, pattern: str) -> dict[str, object]:
        return {
            "payload": payload_name,
            "returncode": 1,
            "stdout": None,
            "stderr": "Invalid/non-supported archive stream",
            "extracted_count": 0,
            "unsupported_error": True,
        }

    monkeypatch.setattr("symx.ota.extract._probe_payload_with_aa", fake_probe_payload_with_aa)

    assert _probe_unsupported_payload_format(artifact) is True


def test_probe_unsupported_payload_format_returns_false_when_payload_extracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = Path("/tmp/test.zip")

    monkeypatch.setattr("symx.ota.extract._ota_is_zip_archive", lambda artifact: True)
    monkeypatch.setattr(
        "symx.ota.extract._read_post_bom_dsc_matches",
        lambda artifact: ["System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64e"],
    )
    monkeypatch.setattr("symx.ota.extract._payload_entry_names", lambda artifact: ["AssetData/payloadv2/payload.003"])
    monkeypatch.setattr(
        "symx.ota.extract._probe_payload_with_aa",
        lambda artifact, payload_name, pattern: {
            "payload": payload_name,
            "returncode": 0,
            "stdout": None,
            "stderr": None,
            "extracted_count": 1,
            "unsupported_error": False,
        },
    )

    assert _probe_unsupported_payload_format(artifact) is False
