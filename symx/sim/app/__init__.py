import logging
import tempfile
from pathlib import Path


import sentry_sdk
import typer
from google.cloud.storage import Client, Bucket
from pydantic import BaseModel, computed_field

from symx.gcs import parse_gcs_url, upload_symbol_binaries
from symx.tools import dyld_split, symsort

logger = logging.getLogger(__name__)
sim_app = typer.Typer()


class SimulatorRuntime(BaseModel):
    arch: str
    build_number: str
    macos_version: str
    os_name: str
    os_version: str
    path: Path

    @computed_field  # type: ignore[misc]
    @property
    def bundle_id(self) -> str:
        return f"sim_{self.macos_version}_{self.os_version}_{self.build_number}_{self.arch}"


_simulator_runtime_prefix = "com.apple.CoreSimulator.SimRuntime."
_dyld_shared_cache_prefix = "dyld_sim_shared_cache_"
_ignored_dyld_file_suffixes = (".map", ".dylddata", ".atlas")


def _dsc_arch_from_file_name(file_name: str) -> str | None:
    path = Path(file_name)
    if not path.name.startswith(_dyld_shared_cache_prefix) or path.suffix in _ignored_dyld_file_suffixes:
        return None
    arch = path.name.removeprefix(_dyld_shared_cache_prefix)
    return arch or None


def _is_ignored_dsc_file(file: Path) -> bool:
    return _dsc_arch_from_file_name(file.name) is None


def _parse_simulator_runtime_name(runtime_name: str) -> tuple[str, str, str] | None:
    if not runtime_name.startswith(_simulator_runtime_prefix):
        return None

    parts = runtime_name.split(".")
    if len(parts) <= 5:
        return None

    os_info = parts[4].split("-")
    if len(os_info) < 3:
        return None

    os_name = os_info[0].lower()
    os_version = ".".join(os_info[1:3])
    build_number = parts[5]
    return os_name, os_version, build_number


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
    with sentry_sdk.start_transaction(op="sim.extract", name="Simulator extract"):
        # todo: move this out to a storage class including meta-data
        uri = parse_gcs_url(storage)
        if uri is None or uri.hostname is None:
            return None
        client: Client = Client(project=uri.username)
        bucket: Bucket = client.bucket(uri.hostname)

        for runtime in find_simulator_runtimes(retrieve_caches_path()):
            with tempfile.TemporaryDirectory(prefix="_sentry_dyld_shared_cache_") as output_dir:
                for dsc_file in runtime.path.iterdir():
                    arch = _dsc_arch_from_file_name(dsc_file.name)
                    if arch is None:
                        continue
                    runtime.arch = arch
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


def find_simulator_runtimes(caches_path: Path) -> list[SimulatorRuntime]:
    runtimes: list[SimulatorRuntime] = []
    for macos_build_path in caches_path.iterdir():
        if macos_build_path.name == ".DS_Store":
            continue
        for runtime_path in macos_build_path.iterdir():
            runtime_info = _parse_simulator_runtime_name(runtime_path.name)
            if runtime_info is None:
                continue
            os_name, os_version, build_number = runtime_info
            for dsc_file in runtime_path.iterdir():
                arch = _dsc_arch_from_file_name(dsc_file.name)
                if arch is None:
                    continue
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
