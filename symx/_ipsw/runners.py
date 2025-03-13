import logging
import shutil
import time
from datetime import timedelta
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


def mirror(ipsw_storage: IpswGcsStorage, timeout: timedelta) -> None:
    start = time.time()
    for artifact in ipsw_storage.artifact_iter(mirror_filter):
        logger.info(f"Downloading {artifact}")
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
        for source_idx, source in enumerate(artifact.sources):
            if int(time.time() - start) > timeout.seconds:
                logger.warning(f"Exiting IPSW mirror due to elapsed timeout of {timeout}")
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
                artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORING_FAILED
                artifact.sources[source_idx].update_last_run()
                ipsw_storage.update_meta_item(artifact)
            else:
                updated_artifact = ipsw_storage.upload_ipsw(artifact, (filepath, source))
                ipsw_storage.update_meta_item(updated_artifact)

            filepath.unlink()


def extract(ipsw_storage: IpswGcsStorage, timeout: timedelta) -> None:
    validate_shell_deps()
    start = time.time()
    for artifact in ipsw_storage.artifact_iter(extract_filter):
        logger.info(f"Processing {artifact.key} for extraction")
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
        for source_idx, source in enumerate(artifact.sources):
            # 1.) Check timeout
            if int(time.time() - start) > timeout.seconds:
                logger.warning(f"Exiting IPSW extract due to elapsed timeout of {timeout}")
                return

            # 2.) Check whether source should be extracted
            sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
            if source.processing_state != ArtifactProcessingState.MIRRORED:
                logger.info(f"Bypassing {source.link} because it isn't ready to extract or already extracted")
                continue

            # 3.) Download IPSW from mirror. If failing update meta-data.
            local_path = ipsw_storage.download_ipsw(source)
            if local_path is None:
                # we haven't been able to download the artifact from the mirror
                artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRROR_CORRUPT
                artifact.sources[source_idx].update_last_run()
                ipsw_storage.update_meta_item(artifact)
                ipsw_storage.clean_local_dir()
                continue

            # 4.) Extract and upload symbols and update meta-data on success or failure.
            try:
                extractor = IpswExtractor(artifact, source, ipsw_storage.local_dir, local_path)
                symbol_binaries_dir = extractor.run()
                ipsw_storage.upload_symbols(
                    extractor.prefix,
                    extractor.bundle_id,
                    artifact,
                    source_idx,
                    symbol_binaries_dir,
                )
                shutil.rmtree(symbol_binaries_dir)
                artifact.sources[source_idx].processing_state = ArtifactProcessingState.SYMBOLS_EXTRACTED
            except Exception as e:
                sentry_sdk.capture_exception(e)
                logger.warning(f"Symbol extraction failed, updating meta-data and continuing with the next one: {e}")
                artifact.sources[source_idx].processing_state = ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
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


def _post_mirrored_filter(  # pyright: ignore [reportUnusedFunction]
    artifacts: Iterable[IpswArtifact],
) -> Sequence[IpswArtifact]:
    return [
        artifact
        for artifact in artifacts
        if any(not _source_post_mirror_condition(source) for source in artifact.sources)
    ]


sources = [
    "UniversalMac_13.5_22G5072a_Restore.ipsw",
    "UniversalMac_13.5_22G5059d_Restore.ipsw",
    "UniversalMac_13.5_22G5048d_Restore.ipsw",
    "UniversalMac_13.5_22G5038d_Restore.ipsw",
    "UniversalMac_13.5_22G5027e_Restore.ipsw",
    "UniversalMac_13.4.1_22F82_Restore.ipsw",
    "UniversalMac_13.4_22F66_Restore.ipsw",
    "UniversalMac_13.4_22F63_Restore.ipsw",
    "UniversalMac_13.4_22F62_Restore.ipsw",
    "UniversalMac_13.4_22F5059b_Restore.ipsw",
    "UniversalMac_13.4_22F5049e_Restore.ipsw",
    "UniversalMac_13.4_22F5037d_Restore.ipsw",
    "UniversalMac_13.4_22F5027f_Restore.ipsw",
    "UniversalMac_13.4.1_22F2083_Restore.ipsw",
    "UniversalMac_13.4_22F2073_Restore.ipsw",
    "UniversalMac_13.3_22E5246b_Restore.ipsw",
    "UniversalMac_13.3_22E5236f_Restore.ipsw",
    "UniversalMac_13.3_22E5230e_Restore.ipsw",
    "UniversalMac_13.3_22E5219e_Restore.ipsw",
    "UniversalMac_13.3.1_22E261_Restore.ipsw",
    "UniversalMac_13.3_22E252_Restore.ipsw",
    "UniversalMac_13.2.1_22D68_Restore.ipsw",
    "UniversalMac_13.2_22D5038i_Restore.ipsw",
    "UniversalMac_13.2_22D5027d_Restore.ipsw",
    "UniversalMac_13.2_22D49_Restore.ipsw",
    "UniversalMac_13.1_22C65_Restore.ipsw",
    "UniversalMac_13.1_22C5059b_Restore.ipsw",
    "UniversalMac_13.1_22C5050e_Restore.ipsw",
    "UniversalMac_13.1_22C5044e_Restore.ipsw",
    "UniversalMac_13.1_22C5033e_Restore.ipsw",
    "UniversalMac_13.0_22A5373b_Restore.ipsw",
    "UniversalMac_13.0_22A5365d_Restore.ipsw",
    "UniversalMac_13.0_22A5358e_Restore.ipsw",
    "UniversalMac_13.0_22A5352e_Restore.ipsw",
    "UniversalMac_13.0_22A5342f_Restore.ipsw",
    "UniversalMac_13.0_22A5331f_Restore.ipsw",
    "UniversalMac_13.0_22A5321d_Restore.ipsw",
    "UniversalMac_13.0_22A5311f_Restore.ipsw",
    "UniversalMac_13.0_22A5295i_Restore.ipsw",
    "UniversalMac_13.0_22A5295h_Restore.ipsw",
    "UniversalMac_13.0_22A5286j_Restore.ipsw",
    "UniversalMac_13.0_22A5266r_Restore.ipsw",
    "UniversalMac_13.0.1_22A400_Restore.ipsw",
    "UniversalMac_13.0_22A380_Restore.ipsw",
    "UniversalMac_13.0_22A379_Restore.ipsw",
    "UniversalMac_13.5_22G74_Restore.ipsw",
    "UniversalMac_13.5_22G74_Restore.ipsw",
    "UniversalMac_13.5.1_22G90_Restore.ipsw",
    "UniversalMac_13.5.2_22G91_Restore.ipsw",
    "UniversalMac_13.6_22G120_Restore.ipsw",
]


def migrate(ipsw_storage: IpswGcsStorage) -> None:
    _, meta_db, _ = ipsw_storage.refresh_artifacts_db()

    for artifact in meta_db.artifacts.values():
        for source_idx, source in enumerate(artifact.sources):
            if source.file_name in sources:
                logger.info(f"\t{source.file_name} ({source.processing_state})")
                sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)

                artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORED
                artifact.sources[source_idx].update_last_run()
                ipsw_storage.update_meta_item(artifact)
