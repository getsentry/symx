import shutil
import subprocess
from pathlib import Path
import logging

from symx._common import Arch
from symx._ipsw.common import IpswArtifact, IpswSource, IpswPlatform

logger = logging.getLogger(__name__)


# TODO: there is good chance that we should split the extractor into separate classes based on at least platform
#   and provide a factory function that instantiates the right extractor class using artifact and maybe source.
class IpswExtractor:
    def __init__(
        self,
        artifact: IpswArtifact,
        source: IpswSource,
        processing_dir: Path,
        ipsw_path: Path,
    ):
        bundle_clean_file_name = source.file_name[:-5].replace(",", "_")
        self.bundle_id = f"ipsw_{bundle_clean_file_name}"
        self.prefix = _map_platform_to_prefix(artifact.platform)
        self.artifact = artifact
        self.source = source
        if not processing_dir.is_dir():
            raise ValueError(
                f"IPSW path is expected to be a directory: {processing_dir}"
            )
        self.processing_dir = processing_dir
        _log_directory_contents(self.processing_dir)

        if not ipsw_path.is_file():
            raise ValueError(f"IPSW path is expected to be a file: {ipsw_path}")

        self.ipsw_path = ipsw_path

    def _ipsw_extract(self, arch: Arch | None = None) -> Path | None:
        command: list[str] = [
            "ipsw",
            "extract",
            str(self.ipsw_path),
            "-d",
            "-o",
            str(self.processing_dir),
        ]

        if arch is not None:
            command.append("-a")
            command.append(str(arch))

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
                # we have very limited space on the GHA runners, so get rid of the source artifact ASAP,
                # but we have to keep the IPSW in case we are extracting multiple architectures
                if arch is None:
                    self.ipsw_path.unlink()

            if process.returncode != 0:
                error_msg = stderr.decode("utf-8")
                raise IpswExtractError(f"ipsw extract failed with {error_msg}")

        _log_directory_contents(self.processing_dir)
        for item in self.processing_dir.iterdir():
            # there should only be IPSW extraction directories or the "split_out" directory if we accumulate over
            # multiple architectures. We shouldn't detect the latter as an input directory to the split function
            if item.is_dir() and str(item.name) != "split_out":
                logger.debug(
                    f"Found {item} in processing directory after IPSW extraction"
                )
                return item

        return None

    def run(self) -> Path:
        if self.artifact.platform == IpswPlatform.MACOS:
            # all macOS IPSWs have dyld_shared_caches for both architectures
            # TODO: figure out from which version this starts to be true
            for arch in [Arch.ARM64E, Arch.X86_64]:
                extract_dir = self._ipsw_extract(arch)
                if extract_dir is None:
                    raise IpswExtractError(
                        "Couldn't find IPSW dyld_shared_cache extraction directory"
                    )
                _log_directory_contents(extract_dir)
                split_dir = self._ipsw_split(extract_dir, arch)
                _log_directory_contents(split_dir)

            # after all architectures have been extracted, we can delete the IPSW to free up some space
            self.ipsw_path.unlink()

            # We accumulate each architecture as a sub-dir in split_dir and let symsorter process them together
        else:
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

    def _ipsw_split(self, extract_dir: Path, arch: Arch | None = None) -> Path:
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
        if arch is not None:
            # each arch gets its own sub-dir, so that the split-dir can be symsorted in one go
            split_dir_arch = split_dir / str(arch)
        else:
            split_dir_arch = split_dir

        result = subprocess.run(
            ["ipsw", "dyld", "split", dsc_root_file, "--output", split_dir_arch],
            capture_output=True,
        )
        # we have very limited space on the GHA runners, so get rid of processed input data
        shutil.rmtree(extract_dir)

        if result.returncode != 0:
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

        if result.returncode != 0:
            raise IpswExtractError(f"Symsorter failed with {result}")

        return output_dir


class IpswExtractError(Exception):
    pass


def _log_directory_contents(directory: Path) -> None:
    if not directory.is_dir():
        return
    dir_contents = "\n".join(str(item.name) for item in directory.iterdir())
    logger.debug(f"Contents of {directory}: \n\n{dir_contents}")


def _map_platform_to_prefix(ipsw_platform: IpswPlatform) -> str:
    # IPSWs differentiate between iPadOS and iOS while OTA doesn't, so we put them in the same prefix
    if ipsw_platform == IpswPlatform.IPADOS:
        prefix_platform = IpswPlatform.IOS
    else:
        prefix_platform = ipsw_platform

    # the symbols store prefixes are all lower-case
    return str(prefix_platform).lower()
