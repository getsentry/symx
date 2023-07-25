import datetime
import logging
import shutil
import subprocess
import time
from pathlib import Path

import sentry_sdk

from symx._common import (
    ArtifactProcessingState,
    download_url_to_file,
    validate_shell_deps,
)
from symx._ipsw.meta_sync.appledb import AppleDbIpswImport
from symx._ipsw.mirror import verify_download
from symx._ipsw.storage.gcs import IpswGcsStorage

logger = logging.getLogger(__name__)


def import_meta_from_appledb(ipsw_storage: IpswGcsStorage) -> None:
    artifacts_meta_blob = ipsw_storage.load_artifacts_meta()
    import_state_blob = ipsw_storage.load_import_state()

    AppleDbIpswImport(ipsw_storage.local_dir).run()

    ipsw_storage.store_artifacts_meta(artifacts_meta_blob)
    ipsw_storage.store_import_state(import_state_blob)


def mirror(ipsw_storage: IpswGcsStorage, timeout: datetime.timedelta) -> None:
    start = time.time()
    for artifact in ipsw_storage.indexed_iter():
        logger.info(f"Downloading {artifact}")
        sentry_sdk.set_tag("ipsw.artifact.key", artifact.key)
        for source in artifact.sources:
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
                continue

            updated_artifact = ipsw_storage.upload_ipsw(artifact, (filepath, source))
            ipsw_storage.update_meta_item(updated_artifact)
            filepath.unlink()


class IpswExtractError(Exception):
    pass


class IpswExtractor:
    def __init__(self, ipsw_path: Path):
        if not ipsw_path.is_file():
            raise ValueError(f"IPSW path is expected to be a file: {ipsw_path}")
        self.ipsw_path = ipsw_path

    def _ipsw_extract(self) -> Path:
        result = subprocess.run(
            ["ipsw", "extract", self.ipsw_path, "-d"],
            capture_output=True,
        )
        if result.returncode == 1:
            raise IpswExtractError(f"ipsw extract failed with {result}")

        # we have very limited space on the GHA runners, so get rid of the source artifact ASAP
        self.ipsw_path.unlink()
        return Path()

    def run(self) -> Path:
        extract_dir = self._ipsw_extract()
        # ipsw split extract_dir split_dir
        split_dir = self._ipsw_split(extract_dir)

        # ./symsorter -zz -o symsort_output --prefix macos --bundle-id 13.0.1_22A00_arm64e symsorter_input
        symsort_dir = self._symsort(split_dir)

        return symsort_dir

    def _ipsw_split(self, extract_dir: Path) -> Path:
        return Path()

    def _symsort(self, split_dir: Path) -> Path:
        return Path()


def extract(ipsw_storage: IpswGcsStorage, timeout: datetime.timedelta) -> None:
    validate_shell_deps()
    start = time.time()
    for artifact in ipsw_storage.mirror_iter():
        logger.info(f"Downloading {artifact} from mirror")
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

            try:
                extractor = IpswExtractor(local_path)
                symbol_binaries_dir = extractor.run()
                ipsw_storage.upload_symbols(artifact, source_idx, symbol_binaries_dir)
                shutil.rmtree(symbol_binaries_dir)
                artifact.sources[source_idx].processing_state = (
                    ArtifactProcessingState.SYMBOLS_EXTRACTED
                )
            except Exception as e:
                sentry_sdk.capture_exception(e)
            finally:
                artifact.sources[source_idx].update_last_run()
                ipsw_storage.update_meta_item(artifact)
