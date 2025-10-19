import logging
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import sentry_sdk
from pydantic import (
    BaseModel,
    computed_field,
    Field,
    field_validator,
    ValidationError,
    HttpUrl,
)

from symx._ipsw.common import (
    IpswReleaseStatus,
    IpswPlatform,
    IpswArtifactHashes,
    IpswArtifact,
    IpswArtifactDb,
    IpswSource,
    ARTIFACTS_META_JSON,
)

APPLEDB_REPO_URL = "https://github.com/littlebyteorg/appledb.git"

logger = logging.getLogger(__name__)


class AppleDbSourceLink(BaseModel):
    url: HttpUrl
    active: bool

    @computed_field  # type: ignore[misc]
    @property
    def preferred(self) -> bool:
        return self.url.scheme == "https"


class AppleDbSource(BaseModel):
    type: str
    devices: list[str] = Field(..., validation_alias="deviceMap")
    links: list[AppleDbSourceLink]
    hashes: IpswArtifactHashes | None = None
    size: int | None = None

    @field_validator("size")
    def size_must_be_a_positive_int(cls, v: int | None) -> int:
        if v is None:
            raise ValueError("We expect size to be not None")
        if v < 0:
            raise ValueError("We expect size to be a non-negative integer")
        return v

    @computed_field  # type: ignore[misc]
    @property
    def link(self) -> HttpUrl | None:
        for link in self.links:
            if link.preferred and link.active:
                return link.url

        return None


class AppleDbArtifact(BaseModel):
    rc: bool | None = None
    beta: bool | None = None
    version: str
    build: str
    released: date | None = None
    sources: list[AppleDbSource] = []

    @field_validator("released", mode="before")
    def empty_string_is_none(cls, v: str) -> str | None:
        if v == "":
            return None
        return v

    @field_validator("version")
    def version_spaces_to_underscore(cls, v: str) -> str:
        return v.replace(" ", "_")

    @computed_field  # type: ignore[misc]
    @property
    def release_status(self) -> IpswReleaseStatus:
        if self.rc:
            return IpswReleaseStatus.RELEASE_CANDIDATE
        elif self.beta:
            return IpswReleaseStatus.BETA

        return IpswReleaseStatus.RELEASE


def ipsw_filename_from_url(url: str) -> str:
    parsed_url = urlparse(url)
    path = parsed_url.path
    return path.split("/")[-1][:-5]


def clone_or_update_appledb_repo(target_dir: Path) -> Path:
    """Clone or update the appledb repository to the target directory."""
    repo_dir = target_dir / "appledb"

    if repo_dir.exists():
        logger.info(f"Updating existing appledb repository at {repo_dir}")
        try:
            subprocess.run(["git", "pull", "--ff-only"], cwd=repo_dir, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            logger.warning(f"Git pull failed, removing and re-cloning: {e}")
            shutil.rmtree(repo_dir)
            return clone_or_update_appledb_repo(target_dir)
    else:
        logger.info(f"Cloning appledb repository to {repo_dir}")
        subprocess.run(
            ["git", "clone", "--depth", "1", APPLEDB_REPO_URL, str(repo_dir)],
            check=True,
            capture_output=True,
            text=True,
        )

    return repo_dir


class AppleDbIpswImport:
    def __init__(self, processing_dir: Path) -> None:
        self._processing_dir = processing_dir
        self._load_meta_db()
        self.processed_file_count = 0
        self.artifact_wo_sources_count = 0
        self.new_artifacts: list[IpswArtifact] = []
        self._repo_dir: Path | None = None

    def run(self) -> None:
        try:
            # Clone or update the appledb repository
            with tempfile.TemporaryDirectory() as temp_dir:
                self._repo_dir = clone_or_update_appledb_repo(Path(temp_dir))

                platforms = list(IpswPlatform)

                # ignore IPod IPSWs when syncing
                platforms.remove(IpswPlatform.IPODOS)

                for platform in platforms:
                    self._process_platform(platform)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            logger.warning(f"Failed to sync IPSW meta-data: {e}")
        finally:
            logger.info(f"Number of processed files = {self.processed_file_count}")
            logger.info(f"Number of artifacts w/o sources = {self.artifact_wo_sources_count}")

    def _store_ipsw_meta(self) -> None:
        with open(self._processing_dir / ARTIFACTS_META_JSON, "w") as fp:
            fp.write(self.meta_db.model_dump_json())

    def _load_meta_db(self) -> None:
        try:
            fp = open(self._processing_dir / ARTIFACTS_META_JSON)
        except IOError:
            self.meta_db = IpswArtifactDb()
        else:
            with fp:
                self.meta_db = IpswArtifactDb.model_validate_json(fp.read())
                logger.info(
                    f"Loaded IPSW meta-data from {self._processing_dir} with {len(self.meta_db.artifacts)} artifacts"
                )

    def _process_platform(self, platform: str) -> None:
        self.current_platform = platform
        sentry_sdk.set_tag("ipsw.import.appledb.platform", platform)

        assert self._repo_dir is not None, "Repository directory must be set"
        platform_dir = self._repo_dir / "osFiles" / platform

        if not platform_dir.exists():
            logger.warning(f"Platform directory {platform_dir} does not exist")
            return

        # Get all items in the platform directory
        platform_items = list(platform_dir.iterdir())

        # Separate folders and files
        folders = [item for item in platform_items if item.is_dir()]
        files = [item for item in platform_items if item.is_file() and item.suffix == ".json"]

        # Process folders (version directories)
        for folder_path in folders:
            folder_name = folder_path.name
            if folder_name in ["0x - Classic"]:
                continue
            self.current_folder_name = folder_name
            sentry_sdk.set_tag("ipsw.import.appledb.folder_name", folder_name)
            self._process_folder(folder_path)

        # Process any files directly in the platform directory
        for file_path in files:
            self._process_file(file_path)

    def _process_folder(self, folder_path: Path) -> None:
        # Get all JSON files in the folder
        json_files = [f for f in folder_path.iterdir() if f.is_file() and f.suffix == ".json"]

        for file_path in json_files:
            self._process_file(file_path)

    def _process_file(self, file_path: Path) -> None:
        logger.info(f"About to process {file_path} in {self.current_platform} folder {self.current_folder_name}")
        self.processed_file_count += 1

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()
            src_artifact = AppleDbArtifact.model_validate_json(file_content)
        except (IOError, OSError) as e:
            logger.error(f"Failed to read file {file_path}: {e}")
            return
        except ValidationError as e:
            sentry_sdk.capture_exception(e)
            logger.warning(f"Failed to validate AppleDb Artifact: {e}")
            return

        # either the artifact has no sources at all...
        if len(src_artifact.sources) == 0:
            self.artifact_wo_sources_count += 1
            logger.warning("IPSW artifact has no sources and won't be imported")
            return

        ipsw_sources: list[IpswSource] = []
        for source in src_artifact.sources:
            if source.link and source.type == "ipsw":
                ipsw_sources.append(IpswSource(**source.model_dump(exclude={"type", "links"})))
        # ...or it has no usable sources (e.g. URLs that are no longer active, non-IPSW source, etc.)
        if len(ipsw_sources) == 0:
            self.artifact_wo_sources_count += 1
            logger.warning("IPSW artifact has no usable sources and won't be imported")
            return

        src_dump = src_artifact.model_dump(exclude={"rc", "beta", "sources"})
        src_dump["platform"] = self.current_platform
        src_dump["sources"] = ipsw_sources
        artifact = IpswArtifact(**src_dump)
        if self.meta_db.contains(artifact.key):
            # this only checks if we already have that id, but it doesn't ask whether they
            # differ... this should be easy to check with pydantic, but it might help to log
            # the diff with something like deepdiff
            logger.warning(
                f"{artifact.key} already added\n\told = {self.meta_db.get(artifact.key)}\n\tnew = {artifact}"
            )
        else:
            self.new_artifacts.append(artifact)
