import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List

import typer
from google.cloud.storage import Client, Bucket

from symx._common import symsort, dyld_split, upload_symbol_binaries, parse_gcs_url

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

    caches_path = os.path.expanduser("~/Library/Developer/CoreSimulator/Caches/dyld")
    if not os.path.isdir(caches_path):
        sys.exit(f"{caches_path} does not exist")

    for runtime in find_simulator_runtimes(caches_path):
        with tempfile.TemporaryDirectory(prefix="_sentry_dyld_shared_cache_") as output_dir:
            for filename in os.listdir(runtime.path):
                if not filename.startswith(_dyld_shared_cache_prefix):
                    continue
                if os.path.splitext(filename)[1] in (".map", ".dylddata"):
                    continue
                runtime.arch = filename.split(_dyld_shared_cache_prefix)[1]
                logging.info(
                    f"Extracting symbols for macOS {runtime.macos_version}, {runtime.os_name} {runtime.os_version} {runtime.arch}"
                )
                extract_system_symbols(runtime, Path(output_dir))

            upload_symbol_binaries(bucket, runtime.os_name, runtime.bundle_id, Path(output_dir))


def find_simulator_runtimes(caches_path: str) -> List[SimulatorRuntime]:
    runtimes: List[SimulatorRuntime] = []
    for macos_version in os.listdir(caches_path):
        if macos_version == ".DS_Store":
            continue
        for sim_runtime_name in os.listdir(os.path.join(caches_path, macos_version)):
            if not sim_runtime_name.startswith(_simulator_runtime_prefix):
                continue
            splits = sim_runtime_name.split(".")
            build_number = splits[5]
            os_info = splits[4].split("-")
            os_version = ".".join(os_info[1:3])
            os_name = os_info[0].lower()
            path = Path(caches_path) / macos_version / sim_runtime_name
            for filename in os.listdir(path):
                if not filename.startswith(_dyld_shared_cache_prefix):
                    continue
                arch = filename.split(_dyld_shared_cache_prefix)[1]
                runtimes.append(
                    SimulatorRuntime(
                        arch=arch,
                        build_number=build_number,
                        macos_version=macos_version,
                        os_name=os_name,
                        os_version=os_version,
                        path=path,
                    )
                )
                break
    return runtimes


def extract_system_symbols(runtime: SimulatorRuntime, output_dir: Path) -> None:
    for filename in os.listdir(runtime.path):
        if not filename.startswith(_dyld_shared_cache_prefix):
            continue
        if os.path.splitext(filename)[1] in (".map", ".dylddata"):
            continue
        with tempfile.TemporaryDirectory(prefix="_sentry_dyld_output") as dsc_out_dir:
            full_path = runtime.path / filename
            split_result = dyld_split(full_path, Path(dsc_out_dir))
            if split_result.returncode != 0:
                raise RuntimeError(f"Failed to split dyld shared cache {full_path}")

            symsort_result = symsort(output_dir, runtime.os_name, runtime.bundle_id, Path(dsc_out_dir))
            if symsort_result.returncode == 0:
                logging.info(f"Extracted symbols for {symsort_result.stdout.decode()}")
