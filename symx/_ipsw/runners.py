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
    "macOS_14.2_beta_2_23C5041e",
    "macOS_14.2_beta_23C5030f",
    "macOS_14.1_23B74",
    "macOS_14.1_RC_23B73",
    "macOS_14.1_beta_3_23B5067a",
    "macOS_14.1_23B2077",
    "macOS_14.1.2_23B92",
    "macOS_14.1.1_23B81",
    "macOS_14.0_23A344",
    "macOS_14.0_RC_2_23A344",
    "macOS_14.0_RC_23A339",
    "macOS_13.6_22G120",
    "macOS_13.5.2_22G91",
    "macOS_13.1_beta_2_22C5044e",
    "macOS_13.1_beta_22C5033e",
    "macOS_13.0_beta_11_22A5373b",
    "macOS_13.0_beta_10_22A5365d",
    "macOS_13.0_beta_9_22A5358e",
    "macOS_13.0_beta_8_22A5352e",
    "macOS_13.0_beta_6_22A5331f",
    "macOS_13.0_beta_5_22A5321d",
    "macOS_13.0_beta_4_22A5311f",
    "macOS_13.0_beta_3_22A5295i",
    "macOS_13.0_beta_3_22A5295h",
    "macOS_13.0_beta_2_22A5286j",
    "macOS_13.0_RC_22A379",
    "macOS_12.6_21G115",
    "macOS_12.6.1_21G217",
    "macOS_12.5_21G72",
    "macOS_12.5_RC_21G69",
    "macOS_12.5_beta_5_21G5063a",
    "macOS_12.5_beta_3_21G5046c",
    "macOS_12.5_beta_2_21G5037d",
    "macOS_12.5.1_21G83",
    "macOS_12.4_21F79",
    "macOS_12.4_beta_4_21F5071b",
    "macOS_12.4_beta_3_21F5063e",
    "macOS_12.4_beta_2_21F5058e",
    "macOS_12.4_beta_21F5048e",
    "macOS_12.4_21F2092",
    "macOS_12.4_21F2081",
    "macOS_12.3_beta_5_21E5227a",
    "macOS_12.3_beta_4_21E5222a",
    "macOS_12.3_beta_3_21E5212f",
    "macOS_12.3_beta_2_21E5206e",
    "macOS_12.3_beta_21E5196i",
    "macOS_12.3_21E230macOS_12.2_beta_2_21D5039d",
    "macOS_12.2_21D49",
    "macOS_12.2_RC_21D48",
]

migrate_source_filenames = [
    "UniversalMac_14.2_23C5041e_Restore.ipsw",
    "UniversalMac_14.2_23C5030f_Restore.ipsw",
    "UniversalMac_14.1_23B74_Restore.ipsw",
    "UniversalMac_14.1_23B73_Restore.ipsw",
    "UniversalMac_14.1_23B5067a_Restore.ipsw",
    "UniversalMac_14.1_23B2077_Restore.ipsw",
    "UniversalMac_14.1.2_23B92_Restore.ipsw",
    "UniversalMac_14.1.1_23B81_Restore.ipsw",
    "UniversalMac_14.0_23A344_Restore.ipsw",
    "UniversalMac_14.0_23A344_Restore.ipsw",
    "UniversalMac_14.0_23A339_Restore.ipsw",
    "UniversalMac_13.6_22G120_Restore.ipsw",
    "UniversalMac_13.5.2_22G91_Restore.ipsw",
    "UniversalMac_13.1_22C5044e_Restore.ipsw",
    "UniversalMac_13.1_22C5033e_Restore.ipsw",
    "UniversalMac_13.0_22A5373b_Restore.ipsw",
    "UniversalMac_13.0_22A5365d_Restore.ipsw",
    "UniversalMac_13.0_22A5358e_Restore.ipsw",
    "UniversalMac_13.0_22A5352e_Restore.ipsw",
    "UniversalMac_13.0_22A5331f_Restore.ipsw",
    "UniversalMac_13.0_22A5321d_Restore.ipsw",
    "UniversalMac_13.0_22A5311f_Restore.ipsw",
    "UniversalMac_13.0_22A5295i_Restore.ipsw",
    "UniversalMac_13.0_22A5295h_Restore.ipsw",
    "UniversalMac_13.0_22A5286j_Restore.ipsw",
    "UniversalMac_13.0_22A379_Restore.ipsw",
    "UniversalMac_12.6_21G115_Restore.ipsw",
    "UniversalMac_12.6.1_21G217_Restore.ipsw",
    "UniversalMac_12.5_21G72_Restore.ipsw",
    "UniversalMac_12.5_21G69_Restore.ipsw",
    "UniversalMac_12.5_21G5063a_Restore.ipsw",
    "UniversalMac_12.5_21G5046c_Restore.ipsw",
    "UniversalMac_12.5_21G5037d_Restore.ipsw",
    "UniversalMac_12.5.1_21G83_Restore.ipsw",
    "UniversalMac_12.4_21F79_Restore.ipsw",
    "UniversalMac_12.4_21F5071b_Restore.ipsw",
    "UniversalMac_12.4_21F5063e_Restore.ipsw",
    "UniversalMac_12.4_21F5058e_Restore.ipsw",
    "UniversalMac_12.4_21F5048e_Restore.ipsw",
    "UniversalMac_12.4_21F2092_Restore.ipsw",
    "UniversalMac_12.4_21F2081_Restore.ipsw",
    "UniversalMac_12.3_21E5227a_Restore.ipsw",
    "UniversalMac_12.3_21E5222a_Restore.ipsw",
    "UniversalMac_12.3_21E5212f_Restore.ipsw",
    "UniversalMac_12.3_21E5206e_Restore.ipsw",
    "UniversalMac_12.3_21E5196i_Restore.ipsw",
    "UniversalMac_12.3_21E230_Restore.ipsw",
    "UniversalMac_12.2_21D5039d_Restore.ipsw",
    "UniversalMac_12.2_21D49_Restore.ipsw",
    "UniversalMac_12.2_21D48_Restore.ipsw",
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
            assert source.processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED

            # TODO: execute below if check successful
            # artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORED
            # artifact.sources[source_idx].update_last_run()
            # ipsw_storage.update_meta_item(artifact)
