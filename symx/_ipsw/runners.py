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
    "AppleTV5,3_17.0_21J5303h_Restore.ipsw",
    "AppleTV5,3_17.0_21J5303f_Restore.ipsw",
    "AppleTV5,3_17.0_21J5293g_Restore.ipsw",
    "AppleTV5,3_16.6_20M5571a_Restore.ipsw",
    "AppleTV5,3_16.6_20M5559c_Restore.ipsw",
    "AppleTV5,3_16.6_20M5548b_Restore.ipsw",
    "AppleTV5,3_16.6_20M5538d_Restore.ipsw",
    "AppleTV5,3_16.6_20M5527e_Restore.ipsw",
    "AppleTV5,3_16.5_20L563_Restore.ipsw",
    "AppleTV5,3_16.5_20L562_Restore.ipsw",
    "AppleTV5,3_16.5_20L5559a_Restore.ipsw",
    "AppleTV5,3_16.5_20L5549e_Restore.ipsw",
    "AppleTV5,3_16.5_20L5538d_Restore.ipsw",
    "AppleTV5,3_16.5_20L5527d_Restore.ipsw",
    "AppleTV5,3_16.4_20L5490a_Restore.ipsw",
    "AppleTV5,3_16.4_20L5480g_Restore.ipsw",
    "AppleTV5,3_16.4_20L5474e_Restore.ipsw",
    "AppleTV5,3_16.4_20L5463g_Restore.ipsw",
    "AppleTV5,3_16.4.1_20L498_Restore.ipsw",
    "AppleTV5,3_16.4_20L497_Restore.ipsw",
    "AppleTV5,3_16.1_20K71_Restore.ipsw",
    "AppleTV5,3_16.3.2_20K672_Restore.ipsw",
    "AppleTV5,3_16.3.1_20K661_Restore.ipsw",
    "AppleTV5,3_16.3_20K650_Restore.ipsw",
    "AppleTV5,3_16.3_20K5637g_Restore.ipsw",
    "AppleTV5,3_16.3_20K5626c_Restore.ipsw",
    "AppleTV5,3_16.2_20K5357b_Restore.ipsw",
    "AppleTV5,3_16.2_20K5348d_Restore.ipsw",
    "AppleTV5,3_16.2_20K5342d_Restore.ipsw",
    "AppleTV5,3_16.2_20K5331f_Restore.ipsw",
    "AppleTV5,3_16.1_20K5068a_Restore.ipsw",
    "AppleTV5,3_16.1_20K5062a_Restore.ipsw",
    "AppleTV5,3_16.1_20K5052c_Restore.ipsw",
    "AppleTV5,3_16.1_20K5046d_Restore.ipsw",
    "AppleTV5,3_16.1_20K5041d_Restore.ipsw",
    "AppleTV5,3_16.2_20K362_Restore.ipsw",
    "AppleTV5,3_16.0_20J5371a_Restore.ipsw",
    "AppleTV5,3_16.0_20J5366a_Restore.ipsw",
    "AppleTV5,3_16.0_20J5355f_Restore.ipsw",
    "AppleTV5,3_16.0_20J5344f_Restore.ipsw",
    "AppleTV5,3_16.0_20J5328g_Restore.ipsw",
    "AppleTV5,3_16.0_20J5319h_Restore.ipsw",
    "AppleTV5,3_16.0_20J373_Restore.ipsw",
    "AppleTV5,3_15.6_19M65_Restore.ipsw",
    "AppleTV5,3_15.6_19M63_Restore.ipsw",
    "AppleTV5,3_15.6_19M5062a_Restore.ipsw",
    "AppleTV5,3_15.6_19M5056c_Restore.ipsw",
    "AppleTV5,3_15.6_19M5046c_Restore.ipsw",
    "AppleTV5,3_15.6_19M5037c_Restore.ipsw",
    "AppleTV5,3_15.6_19M5027c_Restore.ipsw",
    "AppleTV5,3_15.5.1_19L580_Restore.ipsw",
    "AppleTV5,3_15.5_19L570_Restore.ipsw",
    "AppleTV5,3_15.5_19L570_Restore.ipsw",
    "AppleTV5,3_15.5_19L5569a_Restore.ipsw",
    "AppleTV5,3_15.5_19L5562e_Restore.ipsw",
    "AppleTV5,3_15.5_19L5557d_Restore.ipsw",
    "AppleTV5,3_15.5_19L5547e_Restore.ipsw",
    "AppleTV5,3_15.4_19L5440a_Restore.ipsw",
    "AppleTV5,3_15.4_19L5436a_Restore.ipsw",
    "AppleTV5,3_15.4_19L5425e_Restore.ipsw",
    "AppleTV5,3_15.4_19L5419e_Restore.ipsw",
    "AppleTV5,3_15.4_19L5409j_Restore.ipsw",
    "AppleTV5,3_15.4.1_19L452_Restore.ipsw",
    "AppleTV5,3_15.4_19L440_Restore.ipsw",
    "AppleTV5,3_15.4_19L440_Restore.ipsw",
    "AppleTV5,3_15.3_19K5541d_Restore.ipsw",
    "AppleTV5,3_15.3_19K547_Restore.ipsw",
    "AppleTV5,3_15.3_19K545_Restore.ipsw",
    "AppleTV3,2_8.4.3_12H1006_Restore.ipsw",
    "AppleTV5,3_16.6_20M73_Restore.ipsw",
    "AppleTV5,3_16.6_20M73_Restore.ipsw",
    "AppleTV5,3_17.0_21J5318f_Restore.ipsw",
    "AppleTV5,3_17.0_21J5330e_Restore.ipsw",
    "AppleTV5,3_17.0_21J5339b_Restore.ipsw",
    "AppleTV5,3_17.0_21J5353a_Restore.ipsw",
    "AppleTV5,3_17.0_21J5347a_Restore.ipsw",
    "AppleTV5,3_17.0_21J5354a_Restore.ipsw",
    "AppleTV5,3_17.0_21J354_Restore.ipsw",
    "AppleTV5,3_17.0_21J354_Restore.ipsw",
    "AppleTV5,3_17.1_21K5043e_Restore.ipsw",
    "AppleTV5,3_17.1_21K5054e_Restore.ipsw",
    "AppleTV5,3_17.1_21K5064b_Restore.ipsw",
    "AppleTV5,3_17.1_21K69_Restore.ipsw",
    "AppleTV5,3_17.1_21K69_Restore.ipsw",
    "AppleTV5,3_17.2_21K5330g_Restore.ipsw",
    "AppleTV5,3_17.2_21K5341f_Restore.ipsw",
    "AppleTV5,3_17.2_21K5348f_Restore.ipsw",
    "AppleTV5,3_17.2_21K5356c_Restore.ipsw",
    "AppleTV5,3_17.2_21K364_Restore.ipsw",
    "AppleTV5,3_17.2_21K365_Restore.ipsw",
    "AppleTV5,3_17.2_21K365_Restore.ipsw",
    "AppleTV5,3_17.3_21K5625e_Restore.ipsw",
    "AppleTV5,3_17.3_21K5635c_Restore.ipsw",
]


def migrate(ipsw_storage: IpswGcsStorage) -> None:
    _, meta_db, _ = ipsw_storage.refresh_artifacts_db()

    for artifact in meta_db.artifacts.values():
        for source_idx, source in enumerate(artifact.sources):
            if source.file_name in sources:
                logger.info(f"\t{source.file_name} ({source.processing_state})")
                sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
                assert artifact.sources[source_idx].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
                # artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORED
                # artifact.sources[source_idx].update_last_run()
                # ipsw_storage.update_meta_item(artifact)
