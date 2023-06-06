import datetime
import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from math import floor
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterator, Tuple

import requests

from symx._common import Arch, ipsw_version

MiB = 1024 * 1024

logger = logging.getLogger(__name__)

PLATFORMS = [
    "ios",
    "watchos",
    "tvos",
    "audioos",
    "accessory",
    "macos",
    "recovery",
]

ARTIFACTS_META_JSON = "ota_image_meta.json"


class OtaProcessingState(str, Enum):
    # we retrieved metadata from apple and merged it with ours
    INDEXED = "indexed"

    # beta and normal releases are often the exact same file and don't need to be stored or processed twice
    INDEXED_DUPLICATE = "indexed_duplicate"

    # we mirrored that artifact, and it is ready for further processing
    MIRRORED = "mirrored"

    # we failed to retrieve or upload the artifact (OTAs can get unreachable)
    MIRRORING_FAILED = "mirroring_failed"

    # we stored the extracted dyld_shared_cache (optimization, not implemented yet)
    DSC_EXTRACTED = "dsc_extracted"

    # there was no dyld_shared_cache in the OTA, because it was a partial update
    DSC_EXTRACTION_FAILED = "dsc_extraction_failed"

    # the symx goal: symbols are stored for symbolicator to grab
    SYMBOLS_EXTRACTED = "symbols_extracted"

    # this would typically happen when we want to update the symbol store from a given image atomically,
    # and it turns out there are debug-ids already present but with different hash or something similar.
    SYMBOL_EXTRACTION_FAILED = "symbol_extraction_failed"

    # manually assigned to ignore artifact from any processing
    IGNORED = "ignored"


@dataclass
class OtaArtifact:
    build: str
    description: List[str]
    version: str
    platform: str
    id: str
    url: str
    download_path: Optional[str]
    devices: List[str]
    hash: str
    hash_algorithm: str
    last_run: int = 0  # currently the run_id of the GHA Workflow so we can look it up
    processing_state: OtaProcessingState = OtaProcessingState.INDEXED

    def is_indexed(self) -> bool:
        return self.processing_state == OtaProcessingState.INDEXED

    def is_mirrored(self) -> bool:
        return self.processing_state == OtaProcessingState.MIRRORED


OtaMetaData = dict[str, OtaArtifact]


def parse_download_meta_output(
    platform: str,
    result: subprocess.CompletedProcess[bytes],
    meta_data: OtaMetaData,
    beta: bool,
) -> None:
    if result.returncode != 0:
        logger.error(f"Error: {result.stderr!r}")
    else:
        platform_meta = json.loads(result.stdout)
        for meta_item in platform_meta:
            url = meta_item["url"]
            zip_id = url[url.rfind("/") + 1 : -4]
            if len(zip_id) != 40:
                raise RuntimeError(f"Unexpected url-format in {meta_item}")

            if "description" in meta_item:
                desc = [meta_item["description"]]
            else:
                desc = []

            if beta:
                # betas can have the same zip-id as later releases, often with the same contents
                # they only differ by the build. we need to tag them in the key, and we should add
                # a state INDEXED_DUPLICATE as to not process them twice.
                key = zip_id + "_beta"
            else:
                key = zip_id

            meta_data[key] = OtaArtifact(
                id=zip_id,
                build=meta_item["build"],
                description=desc,
                version=meta_item["version"],
                platform=platform,
                url=url,
                devices=meta_item.get("devices", []),
                download_path=None,
                hash=meta_item["hash"],
                hash_algorithm=meta_item["hash_algorithm"],
                processing_state=OtaProcessingState.INDEXED,
                last_run=int(os.getenv("GITHUB_RUN_ID", 0)),
            )


def retrieve_current_meta() -> OtaMetaData:
    meta: OtaMetaData = {}
    for platform in PLATFORMS:
        logger.info(f"Downloading meta for {platform}")
        cmd = [
            "ipsw",
            "download",
            "ota",
            "--platform",
            platform,
            "--urls",
            "--json",
        ]

        parse_download_meta_output(
            platform, subprocess.run(cmd, capture_output=True), meta, False
        )

        beta_cmd = cmd.copy()
        beta_cmd.append("--beta")
        parse_download_meta_output(
            platform, subprocess.run(beta_cmd, capture_output=True), meta, True
        )

    return meta


def merge_lists(a: List[str], b: List[str]) -> List[str]:
    if a is None:
        a = []
    if b is None:
        b = []
    return list(set(a + b))


def merge_meta_data(ours: OtaMetaData, theirs: OtaMetaData) -> None:
    for their_key, their_item in theirs.items():
        if their_key in ours.keys():
            # we already have that id in out meta-store
            our_item = ours[their_key]

            # merge data that can change over time but has no effect on the identity of the artifact
            ours[their_key].description = merge_lists(
                our_item.description, their_item.description
            )
            ours[their_key].devices = merge_lists(our_item.devices, their_item.devices)

            # this is a little bit the core of the whole thing:
            # - what does apple consider identity?
            # - what is sufficient for sentry?
            # - how to migrate if identities change?
            if not (
                their_item.build == our_item.build
                and their_item.version == our_item.version
                and their_item.platform == our_item.platform
                and their_item.url == our_item.url
                and their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
            ):
                raise RuntimeError(
                    f"Same matching keys with different value:\n\tlocal: {our_item}\n\tapple: {their_item}"
                )
        else:
            # it is a new key, store their item in our store
            ours[their_key] = their_item

            # identify and mark beta <-> normal release duplicates
            for our_k, our_v in ours.items():
                if (
                    their_item.hash == our_v.hash
                    and their_item.hash_algorithm == our_v.hash_algorithm
                    and their_item.platform == our_v.platform
                    and their_item.version == our_v.version
                    and their_item.build != our_v.build
                ):
                    ours[
                        their_key
                    ].processing_state = OtaProcessingState.INDEXED_DUPLICATE


def check_hash(ota_meta: OtaArtifact, filepath: Path) -> bool:
    if ota_meta.hash_algorithm != "SHA-1":
        raise RuntimeError(f"Unexpected hash-algo: {ota_meta.hash_algorithm}")

    sha1sum = hashlib.sha1()
    with open(filepath, "rb") as f:
        block = f.read(2**16)
        while len(block) != 0:
            sha1sum.update(block)
            block = f.read(2**16)

    return sha1sum.hexdigest() == ota_meta.hash


def download_ota_from_apple(ota_meta: OtaArtifact, download_dir: Path) -> Path:
    logger.info(f"Downloading {ota_meta}")

    res = requests.get(ota_meta.url, stream=True)
    content_length = res.headers.get("content-length")
    if not content_length:
        raise RuntimeError("OTA Url does not respond with a content-length header")

    total = int(content_length)
    total_mib = total / MiB
    logger.debug(f"OTA Filesize: {floor(total_mib)} MiB")

    # TODO: how much prefix for identity?
    filepath = (
        download_dir
        / f"{ota_meta.platform}_{ota_meta.version}_{ota_meta.build}_{ota_meta.id}.zip"
    )
    with open(filepath, "wb") as f:
        actual = 0
        last_print = 0.0
        for chunk in res.iter_content(chunk_size=8192):
            f.write(chunk)
            actual = actual + len(chunk)

            actual_mib = actual / MiB
            if actual_mib - last_print > 100:
                logger.debug(f"{floor(actual_mib)} MiB")
                last_print = actual_mib

    logger.debug(f"{floor(actual_mib)} MiB")
    if check_hash(ota_meta, filepath):
        logger.info(f"Downloading {ota_meta} completed")
        return filepath

    raise RuntimeError("Failed to download")


def download_ota_from_mirror(ota: OtaArtifact, download_dir: Path) -> Path:
    logger.debug(f"gcs download {ota} to {download_dir}")
    return download_dir / "artifact.zip"


class OtaMirror:
    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self.meta: Dict[Any, Any] = {}

    def update_meta(self) -> None:
        logger.debug("Updating OTA meta-data")
        apple_meta = retrieve_current_meta()
        self.meta = self.storage.save_meta(apple_meta)

    def mirror(self, timeout: datetime.timedelta) -> None:
        logger.debug(f"Mirroring OTA images to {self.storage.bucket.name}")

        start = time.time()
        self.update_meta()
        with tempfile.TemporaryDirectory() as download_dir:
            key: str
            ota: OtaArtifact
            for key, ota in self.meta.items():
                if int(time.time() - start) > timeout.seconds:
                    logger.info(
                        f"Exiting OTA mirror due to elapsed timeout of {timeout}"
                    )
                    return

                if not ota.is_indexed():
                    continue

                ota_file = download_ota_from_apple(ota, Path(download_dir))
                self.storage.save_ota(key, ota, ota_file)
                ota_file.unlink()


DYLD_SHARED_CACHE = "dyld_shared_cache"


@dataclass
class DSCSearchResult:
    arch: Arch
    artifact: Path
    split_dir: Optional[Path]


@dataclass(frozen=True)
class MountInfo:
    dev: str
    id: str
    point: Path


@dataclass(frozen=True)
class ArtifactCategory:
    platform: str
    version: str
    build: str


def validate_shell_deps() -> None:
    version = ipsw_version()
    if version:
        logger.info(f"Using ipsw {version}")
    else:
        logger.error("ipsw not installed")
        sys.exit(1)

    result = subprocess.run(["./symsorter", "--version"], capture_output=True)
    if result.returncode == 0:
        symsorter_version = result.stdout.decode("utf-8")
        logger.info(f"Using {symsorter_version}")
    else:
        # TODO: download symsorter if missing or outdated?
        logger.error("Cannot find symsorter in CWD")
        sys.exit(1)


def patch_cryptex_dmg(artifact: Path, output_dir: Path) -> dict[str, Path]:
    dmg_files = {}
    result = subprocess.run(
        ["ipsw", "ota", "patch", str(artifact), "--output", str(output_dir)],
        capture_output=True,
    )
    if result.returncode == 0 and result.stderr != b"":
        for line in result.stderr.decode("utf-8").splitlines():
            re_match = re.search("Patching (.*) to (.*)", line)
            if re_match:
                dmg_files[re_match.group(1)] = Path(re_match.group(2))

    return dmg_files


def find_system_os_dmgs(search_dir: Path) -> list[Path]:
    result = []
    for artifact in glob.iglob(str(search_dir) + "/**/SystemOS/*.dmg", recursive=True):
        result.append(Path(artifact))
    return result


def parse_hdiutil_mount_output(cmd_output: str) -> MountInfo:
    mount_info = cmd_output.splitlines().pop().split()
    return MountInfo(mount_info[0], mount_info[1], Path(mount_info[2]))


def mount_and_split_dsc(dmg: Path, output_dir: Path) -> list[DSCSearchResult]:
    result = subprocess.run(["hdiutil", "mount", dmg], capture_output=True, check=True)

    mount = parse_hdiutil_mount_output(result.stdout.decode("utf-8"))
    split_dsc_dir = split_dsc(
        mount.point, output_dir / (mount.point.name + "_libraries")
    )
    result = subprocess.run(["hdiutil", "detach", mount.dev], capture_output=True)
    logger.debug(f"\t\t\tResult from detach: {result}")

    return split_dsc_dir


def split_dsc(input_dir: Path, output_dir: Path) -> list[DSCSearchResult]:
    logger.info(f"\t\tSplitting {DYLD_SHARED_CACHE} of {input_dir}")
    dsc_search_results = find_dsc(input_dir)
    for search_result in dsc_search_results:
        # TODO: if we don't create a separate tmp directory here, we are potentially overwriting shit
        search_result.split_dir = output_dir
        # TODO: this file fails kinda silently with two results (test in detail):
        #  /Users/mischan/devel/tmp/ota_downloads/iOS16.3.2_OTAs/AppleTV14,1_a7c3d4ce39aaeebd94e975e0520a0754deff506a.zip
        result = subprocess.run(
            [
                "ipsw",
                "dyld",
                "split",
                search_result.artifact,
                search_result.split_dir,
            ],
            capture_output=True,
        )
        logger.debug(f"\t\t\tResult from split: {result}")
    return dsc_search_results


def find_dsc(input_dir: Path) -> list[DSCSearchResult]:
    # TODO: are we also interested in the DriverKit dyld_shared_cache?
    #  System/DriverKit/System/Library/dyld/
    dsc_path_prefix_options = [
        "System/Library/Caches/com.apple.dyld/",
        "AssetData/payloadv2/patches/System/Library/Caches/com.apple.dyld/",
        "AssetData/payloadv2/ecc_data/System/Library/Caches/com.apple.dyld/",
    ]

    dsc_search_results = []
    for path_prefix in dsc_path_prefix_options:
        for arch in Arch:
            dsc_path = input_dir / (path_prefix + DYLD_SHARED_CACHE + "_" + arch)
            if os.path.isfile(dsc_path):
                dsc_search_results.append(
                    DSCSearchResult(arch=Arch(arch), artifact=dsc_path, split_dir=None)
                )

    if len(dsc_search_results) == 0:
        raise RuntimeError(
            f"Couldn't find any {DYLD_SHARED_CACHE} paths in {input_dir}"
        )
    elif len(dsc_search_results) > 1:
        printable_paths = "\n".join(
            [str(result.artifact) for result in dsc_search_results]
        )
        logger.warning(
            f"Found more than one {DYLD_SHARED_CACHE} path in {input_dir}:\n{printable_paths}"
        )

    return dsc_search_results


def symsort(dsc_split_dir: Path, output_dir: Path, prefix: str, bundle_id: str) -> None:
    logger.info(f"\t\t\tSymsorting {dsc_split_dir} to {output_dir}")
    # TODO: symsorter just writes into the output_path, but in the final scenario we want to change this behavior
    #  to check whether there would be any overwrites, if those overwrites actually contain different content (vs.
    #  just different meta-data) and then atomically do (or not) the write to final output directory.
    # TODO: since this is a terminal node in the process, we should upload the results and add this to OtaExtract

    subprocess.run(
        [
            "./symsorter",
            "-zz",
            "-o",
            output_dir,
            "--prefix",
            prefix,
            "--bundle-id",
            bundle_id,
            dsc_split_dir,
        ],
        check=True,
    )


def find_path_prefix_in_dsc_extract_cmd_output(
    cmd_output: str, top_output_dir: Path
) -> Path:
    for line in cmd_output.splitlines():
        top_output_path_index = line.find(str(top_output_dir))
        if top_output_path_index == -1:
            continue

        extraction_name_start = top_output_path_index + len(str(top_output_dir)) + 1
        extraction_name_end = line.find("/", extraction_name_start)
        if extraction_name_end == -1:
            continue

        return top_output_dir / line[extraction_name_start:extraction_name_end]

    raise RuntimeError(f"Couldn't find path_prefix in command-output: {cmd_output}")


class DscNoMetaData(Exception):
    pass


class DscExtractionFailed(Exception):
    pass


def list_dsc_files(artifact: Path) -> None:
    ps = subprocess.Popen(["ipsw", "ota", "ls", str(artifact)], stdout=subprocess.PIPE)
    try:
        output = subprocess.check_output(("grep", DYLD_SHARED_CACHE), stdin=ps.stdout)
        ps.wait()
        logger.info(output.decode("utf-8"))
    except subprocess.CalledProcessError:
        logger.warning(f"no {DYLD_SHARED_CACHE} found in {str(artifact)}")
        # TODO: in this case we should update the meta-data to reflect processing state
        #       DSC_MISSING, which means this should become an OtaExtract member


# TODO: to be replaced with an update of the corresponding meta-data
def log_artifact_as(name: str, artifact: Path) -> None:
    with open(name, "a") as process_log_file:
        process_log_file.write(str(artifact) + "\n")


class OtaExtract:
    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self.meta: Dict[Any, Any] = {}

    def iter_mirror(self) -> Iterator[Tuple[str, OtaArtifact]]:
        while True:
            key: str
            ota: OtaArtifact
            mirrored_key: Optional[str] = None
            mirrored_ota: Optional[OtaArtifact] = None
            for key, ota in self.storage.load_meta():
                if ota.is_mirrored():
                    logger.debug(f"Found mirrored OTA: {ota}")
                    mirrored_key = key
                    mirrored_ota = ota
                    break

            if mirrored_ota is None or mirrored_key is None:
                # this means we could not find any mirrored OTAs
                logger.warning(f"OTA artifact was not set exiting generator.")
                return
            else:
                logger.debug(
                    f"Yielding mirrored OTA for further processing: {mirrored_ota}"
                )
                yield mirrored_key, mirrored_ota

    def extract(self, timeout: datetime.timedelta) -> None:
        logger.debug(
            f"Extracting symbols from OTA images in {self.storage.bucket.name}"
        )
        start = time.time()
        with tempfile.TemporaryDirectory() as work_dir:
            key: str
            ota: OtaArtifact
            work_dir_path = Path(work_dir)
            for key, ota in self.iter_mirror():
                if int(time.time() - start) > timeout.seconds:
                    logger.warning(
                        f"Exiting OTA extract due to elapsed timeout of {timeout}"
                    )
                    return

                local_ota_path = download_ota_from_mirror(ota, work_dir_path)
                try:
                    self.extract_symbols_from_ota(local_ota_path, ota, work_dir_path)
                    # log_artifact_as("processed", artifact)
                except DscNoMetaData as e:
                    logger.error(e)
                    # log_artifact_as("no_meta_data", artifact)
                except DscExtractionFailed as e:
                    logger.error(e)
                    # log_artifact_as("extraction_failed", artifact)

                local_ota_path.unlink()

    def extract_symbols_from_ota(
        self, artifact: Path, ota: OtaArtifact, output_dir: Path
    ) -> None:
        category = ArtifactCategory(
            ota.platform,
            ota.build,
            ota.version,
        )

        with tempfile.TemporaryDirectory(suffix="_symx") as cryptex_patch_dir:
            logger.info(f"Trying patch_cryptex_dmg with {artifact}")
            extracted_dmgs = patch_cryptex_dmg(artifact, Path(cryptex_patch_dir))
            if len(extracted_dmgs) != 0:
                logger.info(
                    f"\tCryptex patch successful. Mount, split, symsorting {DYLD_SHARED_CACHE} for: {artifact}"
                )
                self.process_cryptex_dmg(extracted_dmgs, category, output_dir)
            else:
                logger.info(
                    f"\tNot a cryptex, so extracting OTA {DYLD_SHARED_CACHE} directly"
                )
                with tempfile.TemporaryDirectory(
                    suffix="_symex"
                ) as extract_dsc_tmp_dir:
                    extracted_dsc_dir = self.extract_ota(
                        artifact, Path(extract_dsc_tmp_dir)
                    )
                    logger.info(
                        f"\t\tSplitting & symsorting {DYLD_SHARED_CACHE} for: {artifact}"
                    )
                    self.split_and_symsort_dsc(extracted_dsc_dir, category, output_dir)

    def extract_ota(self, artifact: Path, output_dir: Path) -> Path:
        result = subprocess.run(
            [
                "ipsw",
                "ota",
                "extract",
                artifact,
                DYLD_SHARED_CACHE,
                "-o",
                output_dir,
            ],
            capture_output=True,
        )
        if result.returncode == 1:
            error_lines = []
            for line in result.stderr.decode("utf-8").splitlines():
                if line.startswith("   ⨯"):
                    error_lines.append(line)
            errors = "\n\t".join(error_lines)
            raise DscExtractionFailed(
                f"Failed to extract {DYLD_SHARED_CACHE} from {artifact}:\n\t{errors}"
            )
        else:
            logger.info(
                f"\t\tSuccessfully extracted {DYLD_SHARED_CACHE} from: {artifact}"
            )
            return find_path_prefix_in_dsc_extract_cmd_output(
                result.stderr.decode("utf-8"),
                Path(output_dir),
            )

    def split_and_symsort_dsc(
        self, input_dir: Path, category: ArtifactCategory, output_dir: Path
    ) -> None:
        with tempfile.TemporaryDirectory(suffix="_symex") as split_temp_dir:
            split_results = split_dsc(
                input_dir,
                Path(split_temp_dir),
            )
            self.symsort_split_results(split_results, category, output_dir)

    def process_cryptex_dmg(
        self,
        extracted_dmgs: dict[str, Path],
        category: ArtifactCategory,
        output_dir: Path,
    ) -> None:
        with tempfile.TemporaryDirectory(suffix="_symex") as mount_and_split_temp_dir:
            split_results = mount_and_split_dsc(
                extracted_dmgs["cryptex-system-arm64e"],
                Path(mount_and_split_temp_dir),
            )
            self.symsort_split_results(split_results, category, output_dir)

    def symsort_split_results(
        self,
        split_cache_results: list[DSCSearchResult],
        category: ArtifactCategory,
        output_dir: Path,
    ) -> None:
        for split_result in split_cache_results:
            if not split_result.split_dir:
                continue

            symsort(
                split_result.split_dir,
                output_dir,
                category.platform,
                f"{category.version}_{category.build}_{split_result.arch.value}",
            )
            # TODO: after the symsorter ran, we could update the meta-data of each debug-id with a ref to the artifact
