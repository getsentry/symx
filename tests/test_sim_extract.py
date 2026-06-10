from pathlib import Path

from symx.sim.app import _dsc_arch_from_file_name, find_simulator_runtimes


def test_dsc_arch_from_file_name_ignores_non_dsc_and_sidecars() -> None:
    assert _dsc_arch_from_file_name("dyld_sim_shared_cache_arm64") == "arm64"
    assert _dsc_arch_from_file_name("dyld_sim_shared_cache_x86_64") == "x86_64"
    assert _dsc_arch_from_file_name("dyld_sim_shared_cache_arm64.map") is None
    assert _dsc_arch_from_file_name("other") is None


def test_find_simulator_runtimes_parses_runtime_name_and_skips_malformed_entries(tmp_path: Path) -> None:
    macos_build = tmp_path / "23F79"
    macos_build.mkdir()
    malformed = macos_build / "com.apple.CoreSimulator.SimRuntime.too-short"
    malformed.mkdir()
    runtime = macos_build / "com.apple.CoreSimulator.SimRuntime.iOS-18-2.22C152"
    runtime.mkdir()
    (runtime / "dyld_sim_shared_cache_arm64").touch()
    (runtime / "dyld_sim_shared_cache_arm64.map").touch()

    runtimes = find_simulator_runtimes(tmp_path)

    assert len(runtimes) == 1
    assert runtimes[0].arch == "arm64"
    assert runtimes[0].build_number == "22C152"
    assert runtimes[0].macos_version == "23F79"
    assert runtimes[0].os_name == "ios"
    assert runtimes[0].os_version == "18.2"
    assert runtimes[0].path == runtime
