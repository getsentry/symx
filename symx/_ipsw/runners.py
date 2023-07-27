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
    download_url_to_file,
    validate_shell_deps,
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
    artifacts_meta_blob = ipsw_storage.load_artifacts_meta()
    import_state_blob = ipsw_storage.load_import_state()

    AppleDbIpswImport(ipsw_storage.local_dir).run()

    ipsw_storage.store_artifacts_meta(artifacts_meta_blob)
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
            download_url_to_file(str(source.link), filepath)
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
        result = subprocess.run(
            ["ipsw", "extract", self.ipsw_path, "-d", "-o", self.processing_dir],
            capture_output=True,
        )
        if result.returncode == 1:
            error_msg = result.stderr.decode("utf-8")
            raise IpswExtractError(f"ipsw extract failed with {error_msg}")

        # we have very limited space on the GHA runners, so get rid of the source artifact ASAP
        self.ipsw_path.unlink()

        _log_directory_contents(self.processing_dir)
        for item in self.processing_dir.iterdir():
            if item.is_dir():
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
            ["ipsw", "dyld", "split", dsc_root_file, split_dir],
            capture_output=True,
        )
        if result.returncode == 1:
            raise IpswExtractError(f"ipsw dyld split failed with {result}")

        # we have very limited space on the GHA runners, so get rid of processed input data
        shutil.rmtree(extract_dir)

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
        if result.returncode == 1:
            raise IpswExtractError(f"Symsorter failed with {result}")

        # we have very limited space on the GHA runners, so get rid of processed input data
        shutil.rmtree(split_dir)

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


def migrate(ipsw_storage: IpswGcsStorage) -> None:
    for artifact in ipsw_storage.artifact_iter(_post_mirrored_filter):
        for source_idx, source in enumerate(artifact.sources):
            if _source_post_mirror_condition(source):
                continue

            assert source.mirror_path is not None
            artifact.sources[source_idx].processing_state = (
                ArtifactProcessingState.MIRRORED
            )
            artifact.sources[source_idx].update_last_run()
        ipsw_storage.update_meta_item(artifact)
