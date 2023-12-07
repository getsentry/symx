import datetime
import logging
import shutil
import time
from typing import Iterable, Sequence

import sentry_sdk

from symx._common import (
    ArtifactProcessingState,
    validate_shell_deps,
    try_download_url_to_file,
)
from symx._ipsw.common import IpswArtifact, IpswSource
from symx._ipsw.extract import IpswExtractor
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


def extract(ipsw_storage: IpswGcsStorage, timeout: datetime.timedelta) -> None:
    validate_shell_deps()
    start = time.time()
    for artifact in ipsw_storage.artifact_iter(extract_filter):
        logger.info(f"Processing {artifact.key} for extraction")
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
        for source_idx, source in enumerate(artifact.sources):
            # 1.) Check timeout
            if int(time.time() - start) > timeout.seconds:
                logger.warning(
                    f"Exiting IPSW extract due to elapsed timeout of {timeout}"
                )
                return

            # 2.) Check whether source should be extracted
            sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
            if source.processing_state != ArtifactProcessingState.MIRRORED:
                logger.info(
                    f"Bypassing {source.link} because it isn't ready to extract or"
                    " already extracted"
                )
                continue

            # 3.) Download IPSW from mirror. If failing update meta-data.
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

            # 4.) Extract and upload symbols and update meta-data on success or failure.
            try:
                extractor = IpswExtractor(
                    artifact, source, ipsw_storage.local_dir, local_path
                )
                symbol_binaries_dir = extractor.run()
                ipsw_storage.upload_symbols(
                    extractor.prefix,
                    extractor.bundle_id,
                    artifact,
                    source_idx,
                    symbol_binaries_dir,
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
