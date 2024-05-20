import datetime
import glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import sentry_sdk

from symx._common import (
    Arch,
    github_run_id,
    ArtifactProcessingState,
    check_sha1,
    validate_shell_deps,
    try_download_url_to_file,
    list_dirs_in,
    rmdir_if_exists,
)

logger = logging.getLogger(__name__)

PLATFORMS = [
    "ios",
    "watchos",
    "tvos",
    "audioos",
    "accessory",
    "macos",
    "recovery",
    "visionos",
]

ARTIFACTS_META_JSON = "ota_image_meta.json"


@dataclass
class OtaArtifact:
    build: str
    description: list[str]
    version: str
    platform: str
    id: str
    url: str
    download_path: str | None
    devices: list[str]
    hash: str
    hash_algorithm: str

    # currently the run_id of the GHA Workflow so we can look it up
    last_run: int = github_run_id()
    processing_state: ArtifactProcessingState = ArtifactProcessingState.INDEXED

    def is_indexed(self) -> bool:
        return self.processing_state == ArtifactProcessingState.INDEXED

    def is_mirrored(self) -> bool:
        return self.processing_state == ArtifactProcessingState.MIRRORED

    def update_last_run(self) -> None:
        self.last_run = github_run_id()


OtaMetaData = dict[str, OtaArtifact]


class OtaStorage(ABC):
    """
    Not an ultra-big fan of this, but this is just here to keep the door open and not fall into circular business.
    Maybe we can get rid of the polymorphic storage in the end, but maybe it makes sense.
    """

    @abstractmethod
    def save_meta(self, theirs: OtaMetaData) -> OtaMetaData:
        raise NotImplementedError()

    @abstractmethod
    def save_ota(self, ota_meta_key: str, ota_meta: OtaArtifact, ota_file: Path) -> None:
        raise NotImplementedError()

    @abstractmethod
    def load_meta(self) -> OtaMetaData | None:
        raise NotImplementedError()

    @abstractmethod
    def load_ota(self, ota: OtaArtifact, download_dir: Path) -> Path | None:
        raise NotImplementedError()

    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError()

    @abstractmethod
    def update_meta_item(self, ota_meta_key: str, ota_meta: OtaArtifact) -> OtaMetaData:
        raise NotImplementedError()

    @abstractmethod
    def upload_symbols(self, input_dir: Path, ota_meta_key: str, ota_meta: OtaArtifact, bundle_id: str) -> None:
        raise NotImplementedError()


class OtaMirrorError(Exception):
    pass


def parse_download_meta_output(
    platform: str,
    result: subprocess.CompletedProcess[bytes],
    meta_data: OtaMetaData,
    beta: bool,
) -> None:
    if result.returncode != 0:
        ipsw_stderr = result.stderr.decode("utf-8")
        # We regularly get 403 errors on the apple endpoint. These seem to be intermittent
        # availability issues and do not warrant error notification noise.
        if "api returned status: 403 Forbidden" not in ipsw_stderr:
            logger.error(f"Download meta failed: {ipsw_stderr}")
    else:
        platform_meta = json.loads(result.stdout)
        for meta_item in platform_meta:
            url = meta_item["url"]
            zip_id_start_idx = url.rfind("/") + 1
            zip_id = url[zip_id_start_idx:-4]
            if len(zip_id) != 40:
                logger.error(f"Parsing download meta: unexpected url-format in {meta_item}")

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

        parse_download_meta_output(platform, subprocess.run(cmd, capture_output=True), meta, False)

        beta_cmd = cmd.copy()
        beta_cmd.append("--beta")
        parse_download_meta_output(platform, subprocess.run(beta_cmd, capture_output=True), meta, True)

    return meta


def merge_lists(a: list[str] | None, b: list[str] | None) -> list[str]:
    if a is None:
        a = []
    if b is None:
        b = []
    return list(set(a + b))


def generate_duplicate_key_from(ours: OtaMetaData, their_key: str) -> str:
    duplicate_num = 1
    key_candidate = f"{their_key}_duplicate_{duplicate_num}"

    while key_candidate in ours.keys():
        duplicate_num += 1
        key_candidate = f"{their_key}_duplicate_{duplicate_num}"

    return key_candidate


def merge_meta_data(ours: OtaMetaData, theirs: OtaMetaData) -> None:
    """
    This function is at the core of the whole thing:
        - What meta-data does Apple consider as identity?
        - What is sufficient "identity" to correctly map to the symbols store?
        - Which merge/duplication strategy should we apply if items turn out to be the same?
        - How to migrate if identities (or our knowledge of identities) change?
    The answers to these questions are encoded in this function.
    :param ours: The meta-data of all OTA artifacts in our store.
    :param theirs: The meta-data of all OTA artifacts currently provided by Apple.
    :return: None
    """
    for their_key, their_item in theirs.items():
        if their_key in ours.keys():
            # we already have that id in out meta-store
            our_item = ours[their_key]

            # merge data that can change over time but has no effect on the identity of the artifact
            ours[their_key].description = merge_lists(our_item.description, their_item.description)
            ours[their_key].devices = merge_lists(our_item.devices, their_item.devices)

            # If we have differing build but all other values that contribute to identity are the same, then we have
            # a duplicate that requires a corresponding duplicate key. Another option would be to merge the builds
            # and have them as an array of a single meta-data item, but that wouldn't really help with the identity
            # back-refs from the store (or any other identity resolution that goes beyond that). For our purposes we
            # should treat this as a separate artifact where we append to the key so that the key-prefix is
            # maintained and set the processing state to INDEXED_DUPLICATE.
            if (
                their_item.build != our_item.build
                and their_item.version == our_item.version
                and their_item.platform == our_item.platform
                and their_item.url == our_item.url
                and their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
            ):
                duplicate_key = generate_duplicate_key_from(ours, their_key)
                ours[duplicate_key] = their_item
                ours[duplicate_key].processing_state = ArtifactProcessingState.INDEXED_DUPLICATE
                continue

            # if any of the remaining identity-contributing values differ at this point then our identity matching is
            # still incomplete.
            if not (
                their_item.build == our_item.build
                and their_item.version == our_item.version
                and their_item.platform == our_item.platform
                and their_item.url == our_item.url
                and their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
            ):
                raise RuntimeError(
                    "Matching keys with different value:\n\tlocal:" f" {our_item}\n\tapple: {their_item}"
                )
        else:
            # it is a new key, store their item in our store
            ours[their_key] = their_item

            # identify and mark beta <-> normal release duplicates
            for _, our_v in ours.items():
                if (
                    their_item.hash == our_v.hash
                    and their_item.hash_algorithm == our_v.hash_algorithm
                    and their_item.platform == our_v.platform
                    and their_item.version == our_v.version
                    and their_item.build != our_v.build
                ):
                    ours[their_key].processing_state = ArtifactProcessingState.INDEXED_DUPLICATE
                    break


def check_ota_hash(ota_meta: OtaArtifact, filepath: Path) -> bool:
    if ota_meta.hash_algorithm != "SHA-1":
        raise RuntimeError(f"Unexpected hash-algo: {ota_meta.hash_algorithm}")

    return check_sha1(ota_meta.hash, filepath)


def download_ota_from_apple(ota_meta: OtaArtifact, download_dir: Path) -> Path:
    logger.info(f"Downloading {ota_meta}")

    filepath = download_dir / f"{ota_meta.platform}_{ota_meta.version}_{ota_meta.build}_{ota_meta.id}.zip"
    try_download_url_to_file(ota_meta.url, filepath)
    if check_ota_hash(ota_meta, filepath):
        logger.info(f"Downloading {ota_meta} completed")
        return filepath

    raise RuntimeError(f"Failed to download {ota_meta.url}")


def set_sentry_artifact_tags(key: str, ota: OtaArtifact) -> None:
    sentry_sdk.set_tag("artifact.key", key)
    sentry_sdk.set_tag("artifact.platform", ota.platform)
    sentry_sdk.set_tag("artifact.version", ota.version)
    sentry_sdk.set_tag("artifact.build", ota.build)


class OtaMirror:
    def __init__(self, storage: OtaStorage) -> None:
        self.storage = storage
        self.meta: OtaMetaData = {}

    def update_meta(self) -> None:
        logger.debug("Updating OTA meta-data")
        apple_meta = retrieve_current_meta()
        self.meta = self.storage.save_meta(apple_meta)

    def mirror(self, timeout: datetime.timedelta) -> None:
        logger.debug(f"Mirroring OTA images to {self.storage.name()}")

        start = time.time()
        self.update_meta()
        with tempfile.TemporaryDirectory() as download_dir:
            key: str
            ota: OtaArtifact
            for key, ota in self.meta.items():
                if int(time.time() - start) > timeout.seconds:
                    logger.info(f"Exiting OTA mirror due to elapsed timeout of {timeout}")
                    return

                if not ota.is_indexed():
                    continue

                set_sentry_artifact_tags(key, ota)
                try:
                    ota_file = download_ota_from_apple(ota, Path(download_dir))
                    self.storage.save_ota(key, ota, ota_file)
                    ota_file.unlink()
                except Exception as e:
                    sentry_sdk.capture_exception(e)
                    logger.exception(e)
                    ota.processing_state = ArtifactProcessingState.INDEXED_INVALID
                    ota.update_last_run()
                    self.storage.update_meta_item(key, ota)


DYLD_SHARED_CACHE = "dyld_shared_cache"


@dataclass(frozen=True)
class DSCSearchResult:
    arch: Arch
    artifact: Path
    split_dir: Path


@dataclass(frozen=True)
class MountInfo:
    dev: str
    id: str
    point: Path


def patch_cryptex_dmg(artifact: Path, output_dir: Path) -> dict[str, Path]:
    dmg_files: dict[str, Path] = {}
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
    result: list[Path] = []
    for artifact in glob.iglob(str(search_dir) + "/**/SystemOS/*.dmg", recursive=True):
        result.append(Path(artifact))
    return result


def parse_hdiutil_mount_output(cmd_output: str) -> MountInfo:
    mount_info = cmd_output.splitlines().pop().split()
    return MountInfo(mount_info[0], mount_info[1], Path(mount_info[2]))


class OtaExtractError(Exception):
    pass


def split_dsc(search_result: list[DSCSearchResult]) -> list[Path]:
    split_dirs: list[Path] = []
    for result_item in search_result:
        logger.info(f"\t\tSplitting {DYLD_SHARED_CACHE} of {result_item.artifact}")
        result = subprocess.run(
            [
                "ipsw",
                "dyld",
                "split",
                str(result_item.artifact),
                "--output",
                str(result_item.split_dir),
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(f"Split for {result_item.artifact} (arch: {result_item.arch} failed:" f" {result}")
        else:
            logger.debug(f"\t\t\tResult from split: {result}")
            split_dirs.append(result_item.split_dir)

    # If none of the split attempts were successful the OTA extraction failed
    if len(split_dirs) == 0:
        artifacts = "\n".join([f"{result_item.artifact}_{result_item.arch}" for result_item in search_result])
        raise OtaExtractError(f"Split failed for all of:\n{artifacts}")

    return split_dirs


def split_dir_exists_in_dsc_search_results(split_dir: Path, dsc_search_result: list[DSCSearchResult]) -> bool:
    for result_item in dsc_search_result:
        if split_dir == result_item.split_dir:
            return True

    return False


def find_dsc(input_dir: Path, ota_meta: OtaArtifact, output_dir: Path) -> list[DSCSearchResult]:
    # TODO: are we also interested in the DriverKit dyld_shared_cache?
    #  System/DriverKit/System/Library/dyld/
    dsc_path_prefix_options = [
        "System/Library/dyld/",
        "System/Library/Caches/com.apple.dyld/",
        "AssetData/payloadv2/patches/System/Library/Caches/com.apple.dyld/",
        "AssetData/payloadv2/ecc_data/System/Library/Caches/com.apple.dyld/",
    ]

    counter = 1
    dsc_search_results: list[DSCSearchResult] = []
    for path_prefix in dsc_path_prefix_options:
        for arch in Arch:
            dsc_path = input_dir / (path_prefix + DYLD_SHARED_CACHE + "_" + arch)
            if os.path.isfile(dsc_path):
                split_dir = output_dir / "split_symbols" / f"{ota_meta.version}_{ota_meta.build}_{arch}"

                if split_dir_exists_in_dsc_search_results(split_dir, dsc_search_results):
                    split_dir = split_dir.parent / f"{split_dir.name}_{counter}"
                    counter = counter + 1

                dsc_search_results.append(DSCSearchResult(arch=Arch(arch), artifact=dsc_path, split_dir=split_dir))

    if len(dsc_search_results) == 0:
        raise OtaExtractError(f"Couldn't find any {DYLD_SHARED_CACHE} paths in {input_dir}")

    return dsc_search_results


def symsort(dsc_split_dir: Path, output_dir: Path, prefix: str, bundle_id: str) -> None:
    logger.info(f"\t\t\tSymsorting {dsc_split_dir} to {output_dir}")

    rmdir_if_exists(output_dir)
    result = subprocess.run(
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
        capture_output=True,
    )
    if result.returncode != 0:
        raise OtaExtractError(f"Symsorter failed with {result}")


def detach_dev(dev: str) -> None:
    result = subprocess.run(["hdiutil", "detach", dev], capture_output=True, check=True)
    logger.debug(f"\t\t\tResult from detach: {result}")


def mount_dmg(dmg: Path) -> MountInfo:
    result = subprocess.run(
        ["hdiutil", "mount", str(dmg)],
        capture_output=True,
        check=True,
    )
    return parse_hdiutil_mount_output(result.stdout.decode("utf-8"))


def extract_ota(artifact: Path, output_dir: Path) -> Path | None:
    subprocess.run(
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

    extract_dirs = list_dirs_in(output_dir)

    if len(extract_dirs) == 0:
        raise OtaExtractError(f"Could not to find {DYLD_SHARED_CACHE} in {artifact}")
    elif len(extract_dirs) > 1:
        extract_dirs_output = "\n".join([str(dir_path) for dir_path in extract_dirs])
        raise OtaExtractError(f"Found more than one image directory in {artifact}:\n{extract_dirs_output}")

    logger.info(f"\t\tSuccessfully extracted {DYLD_SHARED_CACHE} from: {artifact}")

    return extract_dirs[0]


def iter_mirror(storage: OtaStorage) -> Iterator[tuple[str, OtaArtifact]]:
    """
    A generator that reloads the meta-data on every iteration, so we fetch updated mirrored artifacts. This allows
    us to modify the meta-data in the loop that iterates over the output.
    :return: The next current mirrored OtaArtifact to be processed together with its key.
    """
    while True:
        mirrored_key: str | None = None
        mirrored_ota: OtaArtifact | None = None
        ota_meta = storage.load_meta()
        if ota_meta is None:
            logger.error("Could not retrieve meta-data from storage.")
            return

        for key, ota in ota_meta.items():
            if ota.is_mirrored():
                logger.debug(f"Found mirrored OTA: {key}")
                mirrored_key = key
                mirrored_ota = ota
                break

        if mirrored_ota is None or mirrored_key is None:
            # this means we could not find any more mirrored OTAs
            logger.info("No more mirrored OTAs available exiting iter_mirror().")
            return
        else:
            logger.debug(f"Yielding mirrored OTA for further processing: {mirrored_ota}")
            yield mirrored_key, mirrored_ota


class OtaExtract:
    def __init__(self, storage: OtaStorage) -> None:
        self.storage = storage
        self.meta: OtaMetaData = {}

    def extract(self, timeout: datetime.timedelta) -> None:
        validate_shell_deps()

        logger.debug(f"Extracting symbols from OTA images in {self.storage.name()}")
        start = time.time()
        key: str
        ota: OtaArtifact
        for key, ota in iter_mirror(self.storage):
            if int(time.time() - start) > timeout.seconds:
                logger.warning(f"Exiting OTA extract due to elapsed timeout of {timeout}")
                return

            set_sentry_artifact_tags(key, ota)

            with tempfile.TemporaryDirectory() as ota_work_dir:
                work_dir_path = Path(ota_work_dir)
                logger.debug(f"Download mirrored {key} to {work_dir_path}")
                local_ota_path = self.storage.load_ota(ota, work_dir_path)
                if local_ota_path is None:
                    # means there is no OTA at the specified OTA location, although this was defined as MIRRORED
                    # let's set this back to INDEXED, so the mirror workflow tries to download this again.
                    ota.download_path = None
                    ota.processing_state = ArtifactProcessingState.INDEXED
                    ota.update_last_run()
                    self.storage.update_meta_item(key, ota)
                    continue

                try:
                    self.extract_symbols_from_ota(local_ota_path, key, ota, work_dir_path)
                except OtaExtractError as e:
                    # we only "handle" OtaExtractError as something where we can go on, all
                    # other exceptions should just stop the symbol-extraction process.
                    sentry_sdk.capture_exception(e)
                    logger.warning(f"Failed to extract symbols from {ota}: {e}")
                    # also need to mark failing cases, because otherwise they will fail again
                    ota.processing_state = ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
                    ota.update_last_run()
                    self.storage.update_meta_item(key, ota)

    def try_processing_ota_as_cryptex(
        self, local_ota: Path, ota_meta_key: str, ota_meta: OtaArtifact, work_dir: Path
    ) -> bool:
        with tempfile.TemporaryDirectory(suffix="_cryptex_dmg") as cryptex_patch_dir:
            logger.info(f"Trying patch_cryptex_dmg with {local_ota}")
            extracted_dmgs = patch_cryptex_dmg(local_ota, Path(cryptex_patch_dir))
            if len(extracted_dmgs) != 0:
                logger.info(
                    "\tCryptex patch successful. Mount, split, symsorting" f" {DYLD_SHARED_CACHE} for: {local_ota}"
                )
                self.process_cryptex_dmg(extracted_dmgs, ota_meta_key, ota_meta, work_dir)
                # TODO: maybe instead of bool that should be a container of paths produced in work_dir
                return True

        return False

    def process_ota_directly(self, local_ota: Path, ota_meta_key: str, ota_meta: OtaArtifact, work_dir: Path) -> None:
        with tempfile.TemporaryDirectory(suffix="_dsc_extract") as extract_dsc_tmp_dir:
            extracted_dsc_dir = extract_ota(local_ota, Path(extract_dsc_tmp_dir))
            logger.info(f"\t\tSplitting & symsorting {DYLD_SHARED_CACHE} for: {local_ota}")

            if extracted_dsc_dir:
                self.split_and_symsort_dsc(extracted_dsc_dir, ota_meta_key, ota_meta, work_dir)

    def extract_symbols_from_ota(
        self, local_ota: Path, ota_meta_key: str, ota_meta: OtaArtifact, work_dir: Path
    ) -> None:
        if not self.try_processing_ota_as_cryptex(local_ota, ota_meta_key, ota_meta, work_dir):
            logger.info(f"\tNot a cryptex, so extracting OTA {DYLD_SHARED_CACHE} directly")
            self.process_ota_directly(local_ota, ota_meta_key, ota_meta, work_dir)

    def split_and_symsort_dsc(
        self,
        input_dir: Path,
        ota_meta_key: str,
        ota_meta: OtaArtifact,
        output_dir: Path,
    ) -> None:
        split_dirs = split_dsc(find_dsc(input_dir, ota_meta, output_dir))

        self.symsort_split_results(split_dirs, ota_meta_key, ota_meta, output_dir)

    def process_cryptex_dmg(
        self,
        extracted_dmgs: dict[str, Path],
        ota_meta_key: str,
        ota_meta: OtaArtifact,
        output_dir: Path,
    ) -> None:
        mount = mount_dmg(extracted_dmgs["cryptex-system-arm64e"])

        split_dirs = split_dsc(find_dsc(mount.point, ota_meta, output_dir))

        detach_dev(mount.dev)

        self.symsort_split_results(split_dirs, ota_meta_key, ota_meta, output_dir)

    def symsort_split_results(
        self,
        split_dirs: list[Path],
        ota_meta_key: str,
        ota_meta: OtaArtifact,
        output_dir: Path,
    ) -> None:
        for split_dir in split_dirs:
            bundle_id = f"ota_{ota_meta_key}"
            symbols_output_dir = output_dir / "symbols" / bundle_id
            symsort(
                split_dir,
                symbols_output_dir,
                ota_meta.platform,
                bundle_id,
            )
            self.storage.upload_symbols(symbols_output_dir, ota_meta_key, ota_meta, bundle_id)
