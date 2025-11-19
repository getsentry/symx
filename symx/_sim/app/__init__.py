import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List

import typer
from google.cloud.storage import Client, Bucket

from symx._common import symsort, dyld_split, upload_symbol_binaries, parse_gcs_url

logger = logging.getLogger(__name__)
sim_app = typer.Typer()


@dataclass
class SimulatorRuntime:
    arch: str
    build_number: str
    macos_version: str
    os_name: str
    os_version: str
    path: Path

    @property
    def bundle_id(self) -> str:
        return f"sim_{self.macos_version}_{self.os_version}_{self.build_number}_{self.arch}"


_simulator_runtime_prefix = "com.apple.CoreSimulator.SimRuntime."
_dyld_shared_cache_prefix = "dyld_sim_shared_cache_"
_ignored_dyld_file_suffixes = (".map", ".dylddata", ".atlas")


def _is_ignored_dsc_file(file: Path) -> bool:
    return not file.name.startswith(_dyld_shared_cache_prefix) or file.suffix in _ignored_dyld_file_suffixes


@sim_app.command()
def extract(
    storage: str = typer.Option(..., "--storage", "-s", help="URI to a supported storage backend"),
    timeout: int = typer.Option(
        345,
        "--timeout",
        "-t",
        help="timeout in minutes triggering an ordered shutdown after it elapsed",
    ),
) -> None:
    """
    Extract symbols from Simulator images to storage
    """
    # todo: move this out to a storage class including meta-data
    uri = parse_gcs_url(storage)
    if uri is None or uri.hostname is None:
        return None
    client: Client = Client(project=uri.username)
    bucket: Bucket = client.bucket(uri.hostname)

    for runtime in find_simulator_runtimes(retrieve_caches_path()):
        with tempfile.TemporaryDirectory(prefix="_sentry_dyld_shared_cache_") as output_dir:
            for dsc_file in runtime.path.iterdir():
                if _is_ignored_dsc_file(dsc_file):
                    continue
                runtime.arch = dsc_file.name.split(_dyld_shared_cache_prefix)[1]
                logger.info("Extracting symbols for macOS", extra={"runtime": runtime})
                extract_system_symbols(runtime, Path(output_dir))

            upload_symbol_binaries(bucket, runtime.os_name, runtime.bundle_id, Path(output_dir))
            return None
    return None


def retrieve_caches_path() -> Path:
    core_path_str = "/Library/Developer/CoreSimulator/Caches/dyld"
    root_caches_path = Path(core_path_str)
    user_caches_path = Path(f"~{core_path_str}").expanduser()
    # starting with Xcode 16 simulator image caches are store in the root Library folder
    if not root_caches_path.is_dir():
        # up to Xcode 16 simulator image caches were stored per user
        if not user_caches_path.is_dir():
            raise RuntimeError(f"Neither {root_caches_path} nor {user_caches_path} do exist")
        else:
            caches_path = user_caches_path
    else:
        caches_path = root_caches_path
    return caches_path


def find_simulator_runtimes(caches_path: Path) -> List[SimulatorRuntime]:
    runtimes: List[SimulatorRuntime] = []
    for macos_build_path in caches_path.iterdir():
        if macos_build_path.name == ".DS_Store":
            continue
        for runtime_path in macos_build_path.iterdir():
            if not runtime_path.name.startswith(_simulator_runtime_prefix):
                continue
            splits = runtime_path.name.split(".")
            build_number = splits[5]
            os_info = splits[4].split("-")
            os_version = ".".join(os_info[1:3])
            os_name = os_info[0].lower()
            for dsc_file in runtime_path.iterdir():
                if not dsc_file.name.startswith(_dyld_shared_cache_prefix):
                    continue
                arch = dsc_file.name.split(_dyld_shared_cache_prefix)[1]
                runtimes.append(
                    SimulatorRuntime(
                        arch=arch,
                        build_number=build_number,
                        macos_version=macos_build_path.name,
                        os_name=os_name,
                        os_version=os_version,
                        path=runtime_path,
                    )
                )
                # TODO: like in ISPW and OTA, we could try to handle multiple DSCs here instead of breaking after the
                #   first architecture we found. This was taken over from the implementation in the old uploader.
                break
    return runtimes


def extract_system_symbols(runtime: SimulatorRuntime, output_dir: Path) -> None:
    for dsc_file in runtime.path.iterdir():
        if _is_ignored_dsc_file(dsc_file):
            continue
        with tempfile.TemporaryDirectory(prefix="_sentry_dyld_output") as dsc_out_dir:
            split_result = dyld_split(dsc_file, Path(dsc_out_dir))
            if split_result.returncode != 0:
                raise RuntimeError(f"Failed to split dyld shared cache {dsc_file}")

            symsort_result = symsort(output_dir, runtime.os_name, runtime.bundle_id, Path(dsc_out_dir))
            if symsort_result.returncode == 0:
                logger.info("Symsorted symbols.", extra={"symsorter_output": symsort_result.stdout.decode()})
