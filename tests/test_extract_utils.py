"""
Tests for extraction utility functions.

These are pure functions or functions with minimal I/O that support
the OTA and IPSW extraction workflows.
"""

import tempfile
from pathlib import Path

import pytest

from symx._common import Arch, ArtifactProcessingState
from symx._ipsw.common import IpswPlatform
from symx._ipsw.extract import _map_platform_to_prefix
from symx._ota import (
    DSCSearchResult,
    OtaArtifact,
    OtaExtractError,
    find_dsc,
    parse_hdiutil_mount_output,
    split_dir_exists_in_dsc_search_results,
)


# --- _map_platform_to_prefix tests ---


def test_map_platform_ipados_to_ios() -> None:
    """iPadOS and iOS share the same symbol prefix."""
    assert _map_platform_to_prefix(IpswPlatform.IPADOS) == "ios"


def test_map_platform_all_lowercase() -> None:
    """All platform prefixes should be lowercase."""
    for platform in IpswPlatform:
        prefix = _map_platform_to_prefix(platform)
        assert prefix == prefix.lower()


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


# --- split_dir_exists_in_dsc_search_results tests ---


def test_split_dir_exists_finds_match() -> None:
    split_dir = Path("/tmp/split/17.0_21A100_arm64e")
    results = [
        DSCSearchResult(arch=Arch.ARM64E, artifact=Path("/tmp/dsc"), split_dir=split_dir),
    ]
    assert split_dir_exists_in_dsc_search_results(split_dir, results)


def test_split_dir_exists_no_match() -> None:
    results = [
        DSCSearchResult(arch=Arch.ARM64E, artifact=Path("/tmp/dsc"), split_dir=Path("/tmp/other")),
    ]
    assert not split_dir_exists_in_dsc_search_results(Path("/tmp/split"), results)


def test_split_dir_exists_empty_list() -> None:
    assert not split_dir_exists_in_dsc_search_results(Path("/tmp/split"), [])


# --- find_dsc tests ---


def make_ota_artifact() -> OtaArtifact:
    return OtaArtifact(
        id="abc123",
        build="21A100",
        version="17.0",
        platform="ios",
        url="https://example.com/ota.zip",
        hash="abc",
        hash_algorithm="SHA-1",
        description=[],
        devices=[],
        download_path=None,
        processing_state=ArtifactProcessingState.MIRRORED,
    )


def test_find_dsc_standard_path() -> None:
    """Find DSC in System/Library/dyld/ location."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_dir = Path(tmpdir) / "input"
        output_dir = Path(tmpdir) / "output"

        # Create DSC file in standard location
        dsc_dir = input_dir / "System/Library/dyld"
        dsc_dir.mkdir(parents=True)
        (dsc_dir / "dyld_shared_cache_arm64e").touch()

        results = find_dsc(input_dir, make_ota_artifact(), output_dir)

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

        results = find_dsc(input_dir, make_ota_artifact(), output_dir)

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

        results = find_dsc(input_dir, make_ota_artifact(), output_dir)

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
            find_dsc(input_dir, make_ota_artifact(), output_dir)


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

        results = find_dsc(input_dir, make_ota_artifact(), output_dir)

        # Should find both, with unique split_dirs
        assert len(results) == 2
        split_dirs = [r.split_dir for r in results]
        assert len(set(split_dirs)) == 2  # All unique
