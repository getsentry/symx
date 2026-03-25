"""OTA orchestration: mirror, extract workflows, and iteration helpers."""

import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path

import sentry_sdk
import sentry_sdk.metrics

from symx.common import (
    ArtifactProcessingState,
    Timeout,
    validate_shell_deps,
)
from symx.ota.common import (
    DeltaOtaError,
    OtaArtifact,
    OtaDownloader,
    OtaExtractError,
    OtaMetaData,
    OtaMetaRetriever,
    OtaStorage,
    OtaSymbolExtractor,
    RecoveryOtaError,
    parse_version_tuple,
)
from symx.ota.extract import extract_symbols
from symx.ota.meta import retrieve_current_meta
from symx.ota.mirror import download_ota_from_apple

logger = logging.getLogger(__name__)


def _set_artifact_context(key: str, ota: OtaArtifact) -> None:
    """Set sentry tags and structured context for the current artifact."""
    sentry_sdk.set_tag("artifact.key", key)
    sentry_sdk.set_tag("artifact.platform", ota.platform)
    sentry_sdk.set_tag("artifact.version", ota.version)
    sentry_sdk.set_tag("artifact.build", ota.build)
    sentry_sdk.set_context(
        "ota_artifact",
        {
            "key": key,
            "platform": ota.platform,
            "version": ota.version,
            "build": ota.build,
            "url": ota.url,
            "id": ota.id,
            "download_path": ota.download_path,
            "processing_state": str(ota.processing_state),
            "devices": ota.devices,
            "hash": ota.hash,
        },
    )


class OtaMirror:
    def __init__(
        self,
        storage: OtaStorage,
        meta_retriever: OtaMetaRetriever | None = None,
        downloader: OtaDownloader | None = None,
    ) -> None:
        self.storage = storage
        self.meta: OtaMetaData = {}
        self._meta_retriever = meta_retriever if meta_retriever is not None else _RealOtaMetaRetriever()
        self._downloader = downloader if downloader is not None else _RealOtaDownloader()

    def update_meta(self) -> None:
        with sentry_sdk.start_transaction(op="ota.meta_sync", name="OTA meta-sync from Apple"):
            logger.info("Updating OTA meta-data")
            apple_meta = self._meta_retriever.retrieve()
            self.meta = self.storage.save_meta(apple_meta)

    def mirror(self, timer: Timeout) -> None:
        logger.info("Mirroring OTA images to %s", self.storage.name())
        artifacts_mirrored = 0
        artifacts_failed = 0

        self.update_meta()
        with tempfile.TemporaryDirectory() as download_dir:
            key: str
            ota: OtaArtifact
            for key, ota in self.meta.items():
                if timer.exceeded():
                    logger.info("Exiting OTA mirror due to elapsed timeout after %ds", timer.elapsed_seconds)
                    break

                if not ota.is_indexed():
                    continue

                with sentry_sdk.start_transaction(
                    op="ota.mirror",
                    name=f"OTA mirror {ota.platform} {ota.version} {ota.build} ({key[:12]})",
                ):
                    _set_artifact_context(key, ota)
                    try:
                        ota_file = self._downloader.download(ota, Path(download_dir))
                        with sentry_sdk.start_span(op="gcs.upload", name=f"Upload OTA {ota.platform} {ota.version}"):
                            self.storage.save_ota(key, ota, ota_file)
                        ota_file.unlink()
                        artifacts_mirrored += 1
                        sentry_sdk.metrics.count("ota.mirror.succeeded", 1, attributes={"platform": ota.platform})
                    except Exception as e:
                        sentry_sdk.capture_exception(e)
                        logger.exception(e)
                        ota.processing_state = ArtifactProcessingState.INDEXED_INVALID
                        ota.update_last_run()
                        self.storage.update_meta_item(key, ota)
                        artifacts_failed += 1
                        sentry_sdk.metrics.count("ota.mirror.failed", 1, attributes={"platform": ota.platform})

        sentry_sdk.metrics.distribution("ota.mirror.artifacts_mirrored", artifacts_mirrored)
        sentry_sdk.metrics.distribution("ota.mirror.artifacts_failed", artifacts_failed)


def iter_mirror(storage: OtaStorage) -> Iterator[tuple[str, OtaArtifact]]:
    """
    A generator that reloads the meta-data on every iteration, so we fetch updated mirrored artifacts. This allows
    us to modify the meta-data in the loop that iterates over the output.

    Yields the newest mirrored artifact first (by version), so that recent OS releases are prioritized.

    :return: The next current mirrored OtaArtifact to be processed together with its key.
    """
    while True:
        ota_meta = storage.load_meta()
        if ota_meta is None:
            logger.error("Could not retrieve meta-data from storage.")
            return

        mirrored = [(key, ota) for key, ota in ota_meta.items() if ota.is_mirrored()]

        if not mirrored:
            logger.info("No more mirrored OTAs available, exiting iter_mirror().")
            return

        mirrored.sort(key=lambda item: parse_version_tuple(item[1].version), reverse=True)
        mirrored_key, mirrored_ota = mirrored[0]

        logger.info(
            "Processing mirrored OTA %s %s %s (key=%s)",
            mirrored_ota.platform,
            mirrored_ota.version,
            mirrored_ota.build,
            mirrored_key,
        )
        yield mirrored_key, mirrored_ota


class OtaExtract:
    def __init__(
        self,
        storage: OtaStorage,
        extractor: OtaSymbolExtractor | None = None,
    ) -> None:
        self.storage = storage
        self.meta: OtaMetaData = {}
        self._extractor = extractor if extractor is not None else _RealOtaSymbolExtractor()

    def extract(self, timer: Timeout) -> None:
        self._extractor.validate_deps()

        logger.info("Extracting symbols from OTA images on %s", self.storage.name())
        artifacts_extracted = 0
        artifacts_failed = 0
        artifacts_skipped = 0

        key: str
        ota: OtaArtifact
        for key, ota in iter_mirror(self.storage):
            if timer.exceeded():
                logger.warning("Exiting OTA extract due to elapsed timeout after %ds", timer.elapsed_seconds)
                break

            with sentry_sdk.start_transaction(
                op="ota.extract",
                name=f"OTA extract {ota.platform} {ota.version} {ota.build} ({key[:12]})",
            ):
                _set_artifact_context(key, ota)

                with tempfile.TemporaryDirectory() as ota_work_dir:
                    work_dir_path = Path(ota_work_dir)
                    logger.info("Downloading mirrored OTA %s %s %s", ota.platform, ota.version, ota.build)

                    with sentry_sdk.start_span(op="gcs.download", name=f"Download OTA {ota.platform} {ota.version}"):
                        local_ota_path = self.storage.load_ota(ota, work_dir_path)

                    if local_ota_path is None:
                        ota.download_path = None
                        ota.processing_state = ArtifactProcessingState.INDEXED
                        ota.update_last_run()
                        self.storage.update_meta_item(key, ota)
                        continue

                    try:
                        with sentry_sdk.start_span(
                            op="ota.extract.run",
                            name=f"Extract+split+symsort OTA {ota.platform} {ota.version}",
                        ):
                            symbol_dirs = self._extractor.extract(local_ota_path, key, ota, work_dir_path)
                        bundle_id = f"ota_{key}"
                        for symbol_dir in symbol_dirs:
                            with sentry_sdk.start_span(
                                op="gcs.upload_symbols", name=f"Upload OTA symbols {ota.platform} {ota.version}"
                            ):
                                self.storage.upload_symbols(symbol_dir, key, ota, bundle_id)
                        ota.processing_state = ArtifactProcessingState.SYMBOLS_EXTRACTED
                        ota.update_last_run()
                        self.storage.update_meta_item(key, ota)
                        artifacts_extracted += 1
                        sentry_sdk.metrics.count("ota.extract.succeeded", 1, attributes={"platform": ota.platform})
                    except DeltaOtaError:
                        logger.info(
                            "Skipping delta/patch OTA %s %s %s (no full DSC)", ota.platform, ota.version, ota.build
                        )
                        ota.processing_state = ArtifactProcessingState.DELTA_OTA
                        ota.update_last_run()
                        self.storage.update_meta_item(key, ota)
                        artifacts_skipped += 1
                        sentry_sdk.metrics.count("ota.extract.skipped_delta", 1, attributes={"platform": ota.platform})
                    except RecoveryOtaError:
                        logger.info("Skipping recovery OTA %s %s %s (no DSC)", ota.platform, ota.version, ota.build)
                        ota.processing_state = ArtifactProcessingState.RECOVERY_OTA
                        ota.update_last_run()
                        self.storage.update_meta_item(key, ota)
                        artifacts_skipped += 1
                        sentry_sdk.metrics.count(
                            "ota.extract.skipped_recovery", 1, attributes={"platform": ota.platform}
                        )
                    except OtaExtractError as e:
                        sentry_sdk.capture_exception(e)
                        logger.warning(
                            "Failed to extract symbols from OTA %s %s %s", ota.platform, ota.version, ota.build
                        )
                        ota.processing_state = ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
                        ota.update_last_run()
                        self.storage.update_meta_item(key, ota)
                        artifacts_failed += 1
                        sentry_sdk.metrics.count("ota.extract.failed", 1, attributes={"platform": ota.platform})

        sentry_sdk.metrics.distribution("ota.extract.artifacts_extracted", artifacts_extracted)
        sentry_sdk.metrics.distribution("ota.extract.artifacts_failed", artifacts_failed)
        sentry_sdk.metrics.distribution("ota.extract.artifacts_skipped", artifacts_skipped)


# -- Default (production) implementations of injectable interfaces --


class _RealOtaMetaRetriever(OtaMetaRetriever):
    def retrieve(self) -> OtaMetaData:
        return retrieve_current_meta()


class _RealOtaDownloader(OtaDownloader):
    def download(self, ota_meta: OtaArtifact, download_dir: Path) -> Path:
        return download_ota_from_apple(ota_meta, download_dir)


class _RealOtaSymbolExtractor(OtaSymbolExtractor):
    def validate_deps(self) -> None:
        validate_shell_deps()

    def extract(self, local_ota: Path, ota_meta_key: str, ota_meta: OtaArtifact, work_dir: Path) -> list[Path]:
        return extract_symbols(
            local_ota=local_ota,
            platform=ota_meta.platform,
            version=ota_meta.version,
            build=ota_meta.build,
            bundle_id=f"ota_{ota_meta_key}",
            work_dir=work_dir,
        )
