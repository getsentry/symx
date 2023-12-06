import datetime
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable, Sequence

import sentry_sdk

from symx._common import (
    ArtifactProcessingState,
    validate_shell_deps,
    try_download_url_to_file,
)
from symx._ipsw.common import IpswPlatform, IpswArtifact, IpswSource
from symx._ipsw.meta_sync.appledb import AppleDbIpswImport
from symx._ipsw.mirror import verify_download
from symx._ipsw.storage.gcs import (
    IpswGcsStorage,
    mirror_filter,
    extract_filter,
)

logger = logging.getLogger(__name__)


def import_meta_from_appledb(ipsw_storage: IpswGcsStorage) -> None:
    ipsw_storage.load_artifacts_meta()
    import_state_blob = ipsw_storage.load_import_state()

    importer = AppleDbIpswImport(ipsw_storage.local_dir)
    importer.run()

    # the meta store could be updated concurrently by both mirror- and extract-workflows, this means we cannot just
    # write the blob with a generation check, because it will fail in that case without chance for recovery or retry.
    # But since importing is an add-only operation, we can simply collect all artifacts that would be added and then add
    # them individually via update_meta_item() which will always refresh on retry if there was a concurrent update.
    for artifact in importer.new_artifacts:
        ipsw_storage.update_meta_item(artifact)

    # The import state is only updated by the import-workflow, which will never use multiple concurrent runners, so we
    # can use the generation check as a trivial no-retry no-recovery optimistic lock which just fails.
    ipsw_storage.store_import_state(import_state_blob)


def mirror(ipsw_storage: IpswGcsStorage, timeout: datetime.timedelta) -> None:
    start = time.time()
    for artifact in ipsw_storage.artifact_iter(mirror_filter):
        logger.info(f"Downloading {artifact}")
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
        for source_idx, source in enumerate(artifact.sources):
            if int(time.time() - start) > timeout.seconds:
                logger.warning(
                    f"Exiting IPSW mirror due to elapsed timeout of {timeout}"
                )
                return

            sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
            if source.processing_state not in {
                ArtifactProcessingState.INDEXED,
            }:
                logger.info(f"Bypassing {source.link} because it was already mirrored")
                continue

            filepath = ipsw_storage.local_dir / source.file_name
            try_download_url_to_file(str(source.link), filepath)
            if not verify_download(filepath, source):
                artifact.sources[source_idx].processing_state = (
                    ArtifactProcessingState.MIRRORING_FAILED
                )
                artifact.sources[source_idx].update_last_run()
                ipsw_storage.update_meta_item(artifact)
            else:
                updated_artifact = ipsw_storage.upload_ipsw(
                    artifact, (filepath, source)
                )
                ipsw_storage.update_meta_item(updated_artifact)

            filepath.unlink()


class IpswExtractError(Exception):
    pass


def _log_directory_contents(directory: Path) -> None:
    if not directory.is_dir():
        return
    dir_contents = "\n".join(str(item) for item in directory.iterdir())
    logger.debug(f"Contents of {directory}: \n\n{dir_contents}")


class IpswExtractor:
    def __init__(
        self, prefix: str, bundle_id: str, processing_dir: Path, ipsw_path: Path
    ):
        self.prefix = prefix
        self.bundle_id = bundle_id
        if not processing_dir.is_dir():
            raise ValueError(
                f"IPSW path is expected to be a directory: {processing_dir}"
            )
        self.processing_dir = processing_dir
        _log_directory_contents(self.processing_dir)

        if not ipsw_path.is_file():
            raise ValueError(f"IPSW path is expected to be a file: {ipsw_path}")

        self.ipsw_path = ipsw_path

    def _ipsw_extract(self) -> Path | None:
        command: list[str] = [
            "ipsw",
            "extract",
            str(self.ipsw_path),
            "-d",
            "-o",
            str(self.processing_dir),
        ]

        # Start the process using Popen
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as process:
            try:
                # IPSW extraction is typically finished in a couple of minutes. Everything beyond 20 minutes is probably
                # stuck because the dmg mounter asks for a password or something similar.
                stdout, stderr = process.communicate(timeout=(60 * 20))
            except subprocess.TimeoutExpired:
                # the timeout above doesn't kill the process, so make sure it is gone
                process.kill()
                # consume and log remaining output from stdout and stderr
                stdout, _ = process.communicate()
                ipsw_output = stdout.decode("utf-8")
                logger.debug(f"ipsw output: {ipsw_output}")
                raise TimeoutError("IPSW extraction timed out and was terminated.")
            finally:
                # we have very limited space on the GHA runners, so get rid of the source artifact ASAP
                self.ipsw_path.unlink()

            if process.returncode != 0:
                error_msg = stderr.decode("utf-8")
                raise IpswExtractError(f"ipsw extract failed with {error_msg}")

        _log_directory_contents(self.processing_dir)
        for item in self.processing_dir.iterdir():
            if item.is_dir():
                logger.debug(
                    f"Found {item} in processing directory after IPSW extraction"
                )
                return item

        return None

    def run(self) -> Path:
        extract_dir = self._ipsw_extract()
        if extract_dir is None:
            raise IpswExtractError(
                "Couldn't find IPSW dyld_shared_cache extraction directory"
            )
        _log_directory_contents(extract_dir)
        split_dir = self._ipsw_split(extract_dir)
        _log_directory_contents(split_dir)
        symbols_dir = self._symsort(split_dir)
        _log_directory_contents(symbols_dir)

        return symbols_dir

    def _ipsw_split(self, extract_dir: Path) -> Path:
        dsc_root_file = None
        for item in extract_dir.iterdir():
            if (
                item.is_file() and not item.suffix
            ):  # check if it is a file and has no extension
                dsc_root_file = item
                break

        if dsc_root_file is None:
            raise IpswExtractError(
                f"Failed to find dyld_shared_cache root-file in {extract_dir}"
            )
        split_dir = self.processing_dir / "split_out"
        result = subprocess.run(
            ["ipsw", "dyld", "split", dsc_root_file, "--output", split_dir],
            capture_output=True,
        )
        # we have very limited space on the GHA runners, so get rid of processed input data
        shutil.rmtree(extract_dir)

        if result.returncode == 1:
            raise IpswExtractError(f"ipsw dyld split failed with {result}")

        return split_dir

    def _symsort(self, split_dir: Path) -> Path:
        output_dir = self.processing_dir / "symbols"
        logger.info(f"\t\t\tSymsorting {split_dir} to {output_dir}")

        result = subprocess.run(
            [
                "./symsorter",
                "-zz",
                "-o",
                output_dir,
                "--prefix",
                self.prefix,
                "--bundle-id",
                self.bundle_id,
                split_dir,
            ],
            capture_output=True,
        )

        # we have very limited space on the GHA runners, so get rid of processed input data
        shutil.rmtree(split_dir)

        if result.returncode == 1:
            raise IpswExtractError(f"Symsorter failed with {result}")

        return output_dir


def _map_platform_to_prefix(ipsw_platform: IpswPlatform) -> str:
    # IPSWs differentiate between iPadOS and iOS while OTA doesn't, so we put them in the same prefix
    if ipsw_platform == IpswPlatform.IPADOS:
        prefix_platform = IpswPlatform.IOS
    else:
        prefix_platform = ipsw_platform

    # the symbols store prefixes are all lower-case
    return str(prefix_platform).lower()


def extract(ipsw_storage: IpswGcsStorage, timeout: datetime.timedelta) -> None:
    validate_shell_deps()
    start = time.time()
    for artifact in ipsw_storage.artifact_iter(extract_filter):
        logger.info(f"Processing {artifact.key} for extraction")
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
        for source_idx, source in enumerate(artifact.sources):
            if int(time.time() - start) > timeout.seconds:
                logger.warning(
                    f"Exiting IPSW extract due to elapsed timeout of {timeout}"
                )
                return

            sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
            if source.processing_state not in {
                ArtifactProcessingState.MIRRORED,
            }:
                logger.info(
                    f"Bypassing {source.link} because it isn't ready to extract or"
                    " already extracted"
                )
                continue

            local_path = ipsw_storage.download_ipsw(source)
            if local_path is None:
                # we haven't been able to download the artifact from the mirror
                artifact.sources[source_idx].processing_state = (
                    ArtifactProcessingState.MIRROR_CORRUPT
                )
                artifact.sources[source_idx].update_last_run()
                ipsw_storage.update_meta_item(artifact)
                ipsw_storage.clean_local_dir()
                continue

            bundle_clean_file_name = source.file_name[:-5].replace(",", "_")
            bundle_id = f"ipsw_{bundle_clean_file_name}"
            prefix = _map_platform_to_prefix(artifact.platform)
            try:
                extractor = IpswExtractor(
                    prefix, bundle_id, ipsw_storage.local_dir, local_path
                )
                symbol_binaries_dir = extractor.run()
                ipsw_storage.upload_symbols(
                    prefix, bundle_id, artifact, source_idx, symbol_binaries_dir
                )
                shutil.rmtree(symbol_binaries_dir)
                artifact.sources[source_idx].processing_state = (
                    ArtifactProcessingState.SYMBOLS_EXTRACTED
                )
            except Exception as e:
                sentry_sdk.capture_exception(e)
                logger.warning(
                    "Symbol extraction failed, updating meta-data and continuing with"
                    " the next one"
                )
                artifact.sources[source_idx].processing_state = (
                    ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
                )
            finally:
                artifact.sources[source_idx].update_last_run()
                ipsw_storage.update_meta_item(artifact)
                ipsw_storage.clean_local_dir()


def _source_post_mirror_condition(source: IpswSource) -> bool:
    return (
        source.processing_state == ArtifactProcessingState.MIRRORED
        or source.processing_state == ArtifactProcessingState.INDEXED
        or source.processing_state == ArtifactProcessingState.MIRRORING_FAILED
    )


def _post_mirrored_filter(
    artifacts: Iterable[IpswArtifact],
) -> Sequence[IpswArtifact]:
    return [
        artifact
        for artifact in artifacts
        if any(not _source_post_mirror_condition(source) for source in artifact.sources)
    ]


migrate_artifact_keys = [
    "iPadOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iPadOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_16.7.3_RC_20H232",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iPadOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iPadOS_17.1.2_21B101",
    "iPadOS_17.1.2_21B101",
    "iPadOS_17.2_RC_21C62",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_beta_4_21C5054b",
    "iPadOS_16.7.3_RC_20H232",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iPadOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iPadOS_17.1.2_21B101",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iPadOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iPadOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_RC_21C62",
    "iPadOS_17.2_RC_21C62",
    "iPadOS_17.2_RC_21C62",
    "iPadOS_17.2_RC_21C62",
    "iOS_16.7.3_RC_20H232",
    "iPadOS_16.7.3_RC_20H232",
    "iOS_17.2_RC_21C62",
    "iPadOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_16.7.3_RC_20H232",
    "iPadOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iPadOS_17.2_RC_21C62",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iPadOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iPadOS_17.1.2_21B101",
    "iPadOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iPadOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iOS_17.2_RC_21C62",
    "iPadOS_17.2_RC_21C62",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iOS_17.2_beta_4_21C5054b",
    "iPadOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iPadOS_17.1.2_21B101",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iPadOS_17.2_beta_4_21C5054b",
    "iOS_17.1.2_21B101",
    "iPadOS_17.2_beta_4_21C5054b",
    "iPadOS_17.1.2_21B101",
    "iPadOS_17.2_RC_21C62",
    "iPadOS_17.2_RC_21C62",
    "iPadOS_16.7.3_RC_20H232",
]

migrate_source_filenames = [
    "iPad_Fall_2022_17.2_21C62_Restore.ipsw",
    "iPhone14,8_17.2_21C62_Restore.ipsw",
    "iPad14,3,iPad14,4,iPad14,5,iPad14,6_17.2_21C62_Restore.ipsw",
    "iPhone16,2_17.2_21C62_Restore.ipsw",
    "iPhone14,3_17.2_21C62_Restore.ipsw",
    "iPhone10,3,iPhone10,6_16.7.3_20H232_Restore.ipsw",
    "iPhone15,4_17.2_21C5054b_Restore.ipsw",
    "iPhone13,4_17.2_21C5054b_Restore.ipsw",
    "iPhone15,2_17.2_21C5054b_Restore.ipsw",
    "iPhone13,2,iPhone13,3_17.2_21C5054b_Restore.ipsw",
    "iPhone14,8_17.1.2_21B101_Restore.ipsw",
    "iPad_Fall_2022_17.2_21C5054b_Restore.ipsw",
    "iPad_Fall_2022_17.1.2_21B101_Restore.ipsw",
    "iPad14,3,iPad14,4,iPad14,5,iPad14,6_17.2_21C5054b_Restore.ipsw",
    "iPhone14,4_17.1.2_21B101_Restore.ipsw",
    "iPad_Spring_2022_17.2_21C5054b_Restore.ipsw",
    "iPad_10.2_2021_17.1.2_21B101_Restore.ipsw",
    "iPad_Pro_A12X_A12Z_17.1.2_21B101_Restore.ipsw",
    "iPad_10.2_2020_17.2_21C62_Restore.ipsw",
    "iPhone14,3_17.2_21C5054b_Restore.ipsw",
    "iPhone15,3_17.2_21C62_Restore.ipsw",
    "iPhone13,1_17.2_21C5054b_Restore.ipsw",
    "iPadPro_9.7_16.7.3_20H232_Restore.ipsw",
    "iPhone16,1_17.2_21C5054b_Restore.ipsw",
    "iPhone14,7_17.1.2_21B101_Restore.ipsw",
    "iPhone16,1_17.1.2_21B101_Restore.ipsw",
    "iPad_Spring_2019_17.1.2_21B101_Restore.ipsw",
    "iPhone14,5_17.1.2_21B101_Restore.ipsw",
    "iPhone15,4_17.1.2_21B101_Restore.ipsw",
    "iPad_64bit_TouchID_ASTC_17.1.2_21B101_Restore.ipsw",
    "iPhone13,4_17.2_21C62_Restore.ipsw",
    "iPhone15,2_17.2_21C62_Restore.ipsw",
    "iPhone11,8_17.2_21C62_Restore.ipsw",
    "iPhone16,1_17.2_21C62_Restore.ipsw",
    "iPhone15,4_17.2_21C62_Restore.ipsw",
    "iPhone15,3_17.2_21C5054b_Restore.ipsw",
    "iPhone11,2,iPhone11,4,iPhone11,6_17.2_21C5054b_Restore.ipsw",
    "iPhone14,8_17.2_21C5054b_Restore.ipsw",
    "iPhone15,5_17.2_21C5054b_Restore.ipsw",
    "iPhone14,6_17.2_21C5054b_Restore.ipsw",
    "iPhone14,5_17.2_21C5054b_Restore.ipsw",
    "iPhone16,2_17.2_21C5054b_Restore.ipsw",
    "iPhone12,8_17.2_21C5054b_Restore.ipsw",
    "iPhone14,2_17.2_21C5054b_Restore.ipsw",
    "iPhone11,8_17.2_21C5054b_Restore.ipsw",
    "iPad_Fall_2020_17.1.2_21B101_Restore.ipsw",
    "iPad_Fall_2020_17.2_21C5054b_Restore.ipsw",
    "iPhone14,6_17.1.2_21B101_Restore.ipsw",
    "iPhone13,4_17.1.2_21B101_Restore.ipsw",
    "iPhone11,2,iPhone11,4,iPhone11,6_17.1.2_21B101_Restore.ipsw",
    "iPad_Fall_2021_17.2_21C5054b_Restore.ipsw",
    "iPad_Pro_Spring_2021_17.1.2_21B101_Restore.ipsw",
    "iPhone16,2_17.1.2_21B101_Restore.ipsw",
    "iPad_64bit_TouchID_ASTC_17.2_21C5054b_Restore.ipsw",
    "iPhone12,1_17.1.2_21B101_Restore.ipsw",
    "iPhone14,2_17.1.2_21B101_Restore.ipsw",
    "iPad_Pro_HFR_17.2_21C5054b_Restore.ipsw",
    "iPhone13,2,iPhone13,3_17.1.2_21B101_Restore.ipsw",
    "iPhone12,3,iPhone12,5_17.1.2_21B101_Restore.ipsw",
    "iPad_Pro_Spring_2021_17.2_21C5054b_Restore.ipsw",
    "iPhone13,1_17.1.2_21B101_Restore.ipsw",
    "iPad_Pro_Spring_2021_17.2_21C62_Restore.ipsw",
    "iPad_10.2_17.2_21C62_Restore.ipsw",
    "iPad_64bit_TouchID_ASTC_17.2_21C62_Restore.ipsw",
    "iPad_Fall_2020_17.2_21C62_Restore.ipsw",
    "iPhone_4.7_P3_16.7.3_20H232_Restore.ipsw",
    "iPad_64bit_TouchID_ASTC_16.7.3_20H232_Restore.ipsw",
    "iPhone13,1_17.2_21C62_Restore.ipsw",
    "iPad_Spring_2019_17.2_21C62_Restore.ipsw",
    "iPhone13,2,iPhone13,3_17.2_21C62_Restore.ipsw",
    "iPhone_5.5_P3_16.7.3_20H232_Restore.ipsw",
    "iPad_Fall_2021_17.2_21C62_Restore.ipsw",
    "iPhone15,5_17.2_21C62_Restore.ipsw",
    "iPad_Pro_A12X_A12Z_17.2_21C62_Restore.ipsw",
    "iPhone14,7_17.2_21C5054b_Restore.ipsw",
    "iPhone15,2_17.1.2_21B101_Restore.ipsw",
    "iPad_Spring_2022_17.1.2_21B101_Restore.ipsw",
    "iPad_10.2_17.2_21C5054b_Restore.ipsw",
    "iPad14,3,iPad14,4,iPad14,5,iPad14,6_17.1.2_21B101_Restore.ipsw",
    "iPad_Pro_HFR_17.1.2_21B101_Restore.ipsw",
    "iPad_10.2_2020_17.2_21C5054b_Restore.ipsw",
    "iPad_10.2_2020_17.1.2_21B101_Restore.ipsw",
    "iPhone12,8_17.1.2_21B101_Restore.ipsw",
    "iPhone12,8_17.2_21C62_Restore.ipsw",
    "iPhone11,2,iPhone11,4,iPhone11,6_17.2_21C62_Restore.ipsw",
    "iPhone14,2_17.2_21C62_Restore.ipsw",
    "iPhone12,1_17.2_21C62_Restore.ipsw",
    "iPhone14,4_17.2_21C62_Restore.ipsw",
    "iPhone12,3,iPhone12,5_17.2_21C62_Restore.ipsw",
    "iPhone14,7_17.2_21C62_Restore.ipsw",
    "iPhone14,6_17.2_21C62_Restore.ipsw",
    "iPad_Pro_HFR_17.2_21C62_Restore.ipsw",
    "iPhone14,4_17.2_21C5054b_Restore.ipsw",
    "iPhone12,1_17.2_21C5054b_Restore.ipsw",
    "iPhone12,3,iPhone12,5_17.2_21C5054b_Restore.ipsw",
    "iPad_Pro_HFR_17.1.2_21B101_Restore.ipsw",
    "iPhone15,5_17.1.2_21B101_Restore.ipsw",
    "iPad_Fall_2021_17.1.2_21B101_Restore.ipsw",
    "iPhone11,8_17.1.2_21B101_Restore.ipsw",
    "iPad_Pro_A12X_A12Z_17.2_21C5054b_Restore.ipsw",
    "iPad_Spring_2019_17.2_21C5054b_Restore.ipsw",
    "iPhone14,3_17.1.2_21B101_Restore.ipsw",
    "iPad_10.2_2021_17.2_21C5054b_Restore.ipsw",
    "iPad_10.2_17.1.2_21B101_Restore.ipsw",
    "iPad_Spring_2022_17.2_21C62_Restore.ipsw",
    "iPad_10.2_2021_17.2_21C62_Restore.ipsw",
    "iPadPro_12.9_16.7.3_20H232_Restore.ipsw",
]


def migrate(ipsw_storage: IpswGcsStorage) -> None:
    _, meta_db, _ = ipsw_storage.refresh_artifacts_db()
    artifact_key_set = set(migrate_artifact_keys)
    source_filename_set = set(migrate_source_filenames)

    for artifact in meta_db.artifacts.values():
        if artifact.key not in artifact_key_set:
            continue

        logger.info(f"Processing {artifact.key}")
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)

        for source_idx, source in enumerate(artifact.sources):
            if source.file_name not in source_filename_set:
                continue
            logger.info(f"\t{source.file_name}")
            sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
            assert source.processing_state == ArtifactProcessingState.MIRRORED
