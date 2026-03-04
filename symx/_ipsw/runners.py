import logging
import shutil
import time
from datetime import timedelta
from typing import Iterable, Sequence

import sentry_sdk
import sentry_sdk.metrics

from symx._common import (
    ArtifactProcessingState,
    validate_shell_deps,
    try_download_url_to_file,
    log_disk_usage,
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


def _set_artifact_context(artifact: IpswArtifact) -> None:
    """Set sentry tags and structured context for the current artifact."""
    sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
    sentry_sdk.set_tag("ipsw.artifact.platform", str(artifact.platform))
    sentry_sdk.set_tag("ipsw.artifact.version", artifact.version)
    sentry_sdk.set_tag("ipsw.artifact.build", artifact.build)
    sentry_sdk.set_context(
        "ipsw_artifact",
        {
            "key": artifact.key,
            "platform": str(artifact.platform),
            "version": artifact.version,
            "build": artifact.build,
            "released": str(artifact.released) if artifact.released else None,
            "sources": [
                {
                    "file_name": s.file_name,
                    "link": str(s.link),
                    "processing_state": str(s.processing_state),
                    "mirror_path": s.mirror_path,
                    "size": s.size,
                }
                for s in artifact.sources
            ],
        },
    )


def import_meta_from_appledb(ipsw_storage: IpswGcsStorage) -> None:
    with sentry_sdk.start_span(op="ipsw.meta_sync", name="IPSW meta-sync from AppleDB"):
        ipsw_storage.load_artifacts_meta()

        importer = AppleDbIpswImport(ipsw_storage.local_dir)
        importer.run()

        logger.info("Updating IPSW meta with %d new artifacts", len(importer.new_artifacts))
        sentry_sdk.metrics.distribution("ipsw.meta_sync.new_artifacts", len(importer.new_artifacts))

        with sentry_sdk.start_span(op="ipsw.meta_sync.upsert", name="Upsert new artifacts") as upsert_span:
            upsert_span.set_data("count", len(importer.new_artifacts))
            for artifact in importer.new_artifacts:
                ipsw_storage.update_meta_item(artifact)


def mirror(ipsw_storage: IpswGcsStorage, timeout: timedelta) -> None:
    start = time.time()
    artifacts_mirrored = 0
    artifacts_failed = 0

    for artifact in ipsw_storage.artifact_iter(mirror_filter):
        with sentry_sdk.start_transaction(
            op="ipsw.mirror",
            name=f"IPSW mirror {artifact.platform} {artifact.version} {artifact.build}",
        ):
            _set_artifact_context(artifact)
            logger.info("Mirroring artifact %s", artifact.key)

            for source_idx, source in enumerate(artifact.sources):
                if int(time.time() - start) > timeout.seconds:
                    logger.warning("Exiting IPSW mirror due to elapsed timeout after %ds", int(time.time() - start))
                    sentry_sdk.metrics.distribution("ipsw.mirror.artifacts_mirrored", artifacts_mirrored)
                    sentry_sdk.metrics.distribution("ipsw.mirror.artifacts_failed", artifacts_failed)
                    return

                sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
                if source.processing_state not in {
                    ArtifactProcessingState.INDEXED,
                }:
                    logger.info("Bypassing %s (already %s)", source.file_name, source.processing_state)
                    continue

                with sentry_sdk.start_span(
                    op="ipsw.mirror.source",
                    name=f"Mirror source {source.file_name}",
                ) as source_span:
                    source_span.set_data("source.file_name", source.file_name)

                    log_disk_usage()
                    filepath = ipsw_storage.local_dir / source.file_name

                    with sentry_sdk.start_span(op="http.download", name=f"Download {source.file_name} from Apple"):
                        try_download_url_to_file(str(source.link), filepath)

                    with sentry_sdk.start_span(op="ipsw.mirror.verify", name=f"Verify {source.file_name}"):
                        download_ok = verify_download(filepath, source)

                    if not download_ok:
                        artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORING_FAILED
                        artifact.sources[source_idx].update_last_run()
                        ipsw_storage.update_meta_item(artifact)
                        source_span.set_status("internal_error")
                        artifacts_failed += 1
                        sentry_sdk.metrics.count(
                            "ipsw.mirror.failed", 1, attributes={"platform": str(artifact.platform)}
                        )
                    else:
                        with sentry_sdk.start_span(op="gcs.upload", name=f"Upload {source.file_name} to GCS"):
                            updated_artifact = ipsw_storage.upload_ipsw(artifact, (filepath, source))
                        ipsw_storage.update_meta_item(updated_artifact)
                        artifacts_mirrored += 1
                        sentry_sdk.metrics.count(
                            "ipsw.mirror.succeeded", 1, attributes={"platform": str(artifact.platform)}
                        )

                    filepath.unlink()

    sentry_sdk.metrics.distribution("ipsw.mirror.artifacts_mirrored", artifacts_mirrored)
    sentry_sdk.metrics.distribution("ipsw.mirror.artifacts_failed", artifacts_failed)


def extract(ipsw_storage: IpswGcsStorage, timeout: timedelta) -> None:
    validate_shell_deps()
    start = time.time()
    artifacts_extracted = 0
    artifacts_failed = 0

    for artifact in ipsw_storage.artifact_iter(extract_filter):
        with sentry_sdk.start_transaction(
            op="ipsw.extract",
            name=f"IPSW extract {artifact.platform} {artifact.version} {artifact.build}",
        ):
            _set_artifact_context(artifact)
            logger.info(
                "Extracting artifact %s (%s %s %s)",
                artifact.key,
                artifact.platform,
                artifact.version,
                artifact.build,
            )

            for source_idx, source in enumerate(artifact.sources):
                # 1.) Check timeout
                if int(time.time() - start) > timeout.seconds:
                    logger.warning("Exiting IPSW extract due to elapsed timeout after %ds", int(time.time() - start))
                    sentry_sdk.metrics.distribution("ipsw.extract.artifacts_extracted", artifacts_extracted)
                    sentry_sdk.metrics.distribution("ipsw.extract.artifacts_failed", artifacts_failed)
                    return

                # 2.) Check whether source should be extracted
                sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
                if source.processing_state != ArtifactProcessingState.MIRRORED:
                    logger.info("Bypassing %s (state=%s)", source.file_name, source.processing_state)
                    continue

                with sentry_sdk.start_span(
                    op="ipsw.extract.source",
                    name=f"Extract source {source.file_name}",
                ) as source_span:
                    source_span.set_data("source.file_name", source.file_name)

                    # 3.) Download IPSW from mirror. If failing update meta-data.
                    log_disk_usage()
                    with sentry_sdk.start_span(op="gcs.download", name=f"Download {source.file_name} from mirror"):
                        local_path = ipsw_storage.download_ipsw(source)

                    if local_path is None:
                        artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRROR_CORRUPT
                        artifact.sources[source_idx].update_last_run()
                        ipsw_storage.update_meta_item(artifact)
                        ipsw_storage.clean_local_dir()
                        source_span.set_status("internal_error")
                        artifacts_failed += 1
                        sentry_sdk.metrics.count(
                            "ipsw.extract.mirror_corrupt", 1, attributes={"platform": str(artifact.platform)}
                        )
                        continue

                    # 4.) Extract and upload symbols and update meta-data on success or failure.
                    try:
                        with sentry_sdk.start_span(
                            op="ipsw.extract.run", name=f"IPSW extract+symsort {source.file_name}"
                        ):
                            extractor = IpswExtractor(
                                artifact.platform, source.file_name, ipsw_storage.local_dir, local_path
                            )
                            symbol_binaries_dir = extractor.run()

                        with sentry_sdk.start_span(
                            op="gcs.upload_symbols", name=f"Upload symbols for {source.file_name}"
                        ):
                            ipsw_storage.upload_symbols(
                                extractor.prefix,
                                extractor.bundle_id,
                                artifact,
                                source_idx,
                                symbol_binaries_dir,
                            )

                        shutil.rmtree(symbol_binaries_dir)
                        artifact.sources[source_idx].processing_state = ArtifactProcessingState.SYMBOLS_EXTRACTED
                        artifacts_extracted += 1
                        sentry_sdk.metrics.count(
                            "ipsw.extract.succeeded", 1, attributes={"platform": str(artifact.platform)}
                        )
                    except Exception as e:
                        sentry_sdk.capture_exception(e)
                        logger.warning(
                            "Symbol extraction failed for %s, continuing with the next one.",
                            source.file_name,
                            extra={"artifact": artifact, "source": source, "exception": e},
                        )
                        artifact.sources[source_idx].processing_state = ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
                        source_span.set_status("internal_error")
                        artifacts_failed += 1
                        sentry_sdk.metrics.count(
                            "ipsw.extract.failed", 1, attributes={"platform": str(artifact.platform)}
                        )
                    finally:
                        artifact.sources[source_idx].update_last_run()
                        ipsw_storage.update_meta_item(artifact)
                        ipsw_storage.clean_local_dir()

    sentry_sdk.metrics.distribution("ipsw.extract.artifacts_extracted", artifacts_extracted)
    sentry_sdk.metrics.distribution("ipsw.extract.artifacts_failed", artifacts_failed)


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
    "UniversalMac_26.3_25D5112c_Restore.ipsw",
    "AppleTV5,3_26.3_23K5611c_Restore.ipsw",
    "Apple_Vision_Pro_26.3_23N5613b_Restore.ipsw",
    "UniversalMac_26.3_25D125_Restore.ipsw",
    "Apple_Vision_Pro_26.3_23N620_Restore.ipsw",
    "Apple_Vision_Pro_26.4_23O5209m_Restore.ipsw",
    "UniversalMac_26.4_25E5218f_Restore.ipsw",
    "Apple_Vision_Pro_26.4_23O5220e_Restore.ipsw",
]


def migrate(ipsw_storage: IpswGcsStorage) -> None:
    _, meta_db, _ = ipsw_storage.refresh_artifacts_db()

    for artifact in meta_db.artifacts.values():
        for source_idx, source in enumerate(artifact.sources):
            if source.file_name in sources:
                logger.info("\t%s (%s)" % (source.file_name, source.processing_state))
                sentry_sdk.set_tag("ipsw.artifact.source", source.file_name)
                if artifact.sources[source_idx].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED:
                    logger.info("\tChanging %s to %s" % (source.file_name, ArtifactProcessingState.MIRRORED))
                    artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORED
                    artifact.sources[source_idx].update_last_run()
                    ipsw_storage.update_meta_item(artifact)
