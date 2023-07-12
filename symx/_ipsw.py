import json
import logging
import random
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from urllib.parse import urlparse
from symx._common import ArtifactProcessingState

import requests
from pydantic import (
    BaseModel,
    computed_field,
    Field,
    field_validator,
    ValidationError,
    HttpUrl,
)
from pydantic_core.core_schema import FieldValidationInfo

logger = logging.getLogger(__name__)


class IpswReleaseStatus(StrEnum):
    RELEASE = "rel"
    RELEASE_CANDIDATE = "rc"
    BETA = "beta"


class IpswPlatform(StrEnum):
    AUDIOOS = "audioOS"
    BRIDGEOS = "bridgeOS"
    IOS = "iOS"
    IPADOS = "iPadOS"
    IPODOS = "iPodOS"
    MACOS = "macOS"
    TVOS = "tvOS"
    WATCHOS = "watchOS"


class IpswArtifactHashes(BaseModel):
    sha1: str | None = None
    sha2: str | None = Field(None, validation_alias="sha2-256")


class AppleDbSourceLink(BaseModel):
    url: HttpUrl
    preferred: bool
    active: bool


class AppleDbSource(BaseModel):
    type: str
    devices: list[str] = Field(..., validation_alias="deviceMap")
    links: list[AppleDbSourceLink]
    hashes: IpswArtifactHashes | None = None
    size: int | None = None

    @field_validator("size")
    def size_should_never_be_negative(cls, v: int, _: FieldValidationInfo) -> int:
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


class IpswSource(BaseModel):
    devices: list[str]
    link: HttpUrl
    hashes: IpswArtifactHashes | None = None
    size: int | None = None


class IpswArtifact(BaseModel):
    platform: IpswPlatform
    version: str
    build: str
    released: date | None = None
    release_status: IpswReleaseStatus
    sources: list[IpswSource]
    processing_state: ArtifactProcessingState = ArtifactProcessingState.INDEXED

    @computed_field  # type: ignore[misc]
    @property
    def key(self) -> str:
        return f"{self.platform}_{self.version}_{self.build}"


class AppleDbArtifact(BaseModel):
    rc: bool | None = None
    beta: bool | None = None
    version: str
    build: str
    released: date | None = None
    sources: list[AppleDbSource] = []

    @field_validator("version")
    def version_spaces_to_underscore(cls, v: str, _: FieldValidationInfo) -> str:
        return v.replace(" ", "_")

    @computed_field  # type: ignore[misc]
    @property
    def release_status(self) -> IpswReleaseStatus:
        if self.rc is True:
            return IpswReleaseStatus.RELEASE_CANDIDATE
        elif self.beta is True:
            return IpswReleaseStatus.BETA

        return IpswReleaseStatus.RELEASE


def ipsw_filename_from_url(url: str) -> str:
    parsed_url = urlparse(url)
    path = parsed_url.path
    return path.split("/")[-1][:-5]


AppleDBRepoURL = "https://github.com/littlebyteorg/appledb"
ApiContentsURL = "https://api.github.com/repos/littlebyteorg/appledb/contents/"


def random_user_agent() -> str:
    user_agents = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like"
            " Gecko) Chrome/61.0.3163.100 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like"
            " Gecko) Chrome/61.0.3163.100 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_6) AppleWebKit/537.36 (KHTML,"
            " like Gecko) Chrome/61.0.3163.100 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_6) AppleWebKit/604.1.38"
            " (KHTML, like Gecko) Version/11.0 Safari/604.1.38"
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:56.0) Gecko/20100101"
            " Firefox/56.0"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13) AppleWebKit/604.1.38 (KHTML,"
            " like Gecko) Version/11.0 Safari/604.1.38"
        ),
    ]
    return random.choice(user_agents)


class IpswArtifactDb(BaseModel):
    version: int = 0
    artifacts: dict[str, IpswArtifact] = {}

    def contains(self, key: str) -> bool:
        return key in self.artifacts


@dataclass
class AppleDbIspwImportState:
    platform: str | None = None
    folder_hash: str | None = None
    file_hash: str | None = None


class AppleDbIspwImport:
    def __init__(self) -> None:
        self._load_appledb_indexed()
        self._load_meta_db()
        self.request_count = 0
        self.state = AppleDbIspwImportState()

    def run(self) -> None:
        try:
            for platform in IpswPlatform:
                self._process_platform(platform)
        finally:
            print(f"Meta-DB:\n\n{self.meta_db.model_dump_json(indent=4)}")
            print(f"Number of github API requests = {self.request_count}")

            self._store_appledb_indexed()
            self._store_ipsw_meta()

    def _store_ipsw_meta(self) -> None:
        with open("ipsw_meta.json", "w") as fp:
            fp.write(self.meta_db.model_dump_json())

    def _store_appledb_indexed(self) -> None:
        with open("appledb_import_state.json", "w") as fp:
            json.dump(self.apple_db_import_state, fp)

    def _load_meta_db(self) -> None:
        try:
            fp = open("ipsw_meta.json")
        except IOError:
            self.meta_db = IpswArtifactDb()
        else:
            with fp:
                self.meta_db = IpswArtifactDb.model_validate_json(fp.read())

    def _load_appledb_indexed(self) -> None:
        try:
            fp = open("appledb_import_state.json")
        except IOError:
            self.apple_db_import_state: dict[str, list[str]] = {}
        else:
            with fp:
                self.apple_db_import_state = json.load(fp)

    def _process_platform(self, platform: str) -> None:
        self.state.platform = platform
        platform_url = f"{ApiContentsURL}osFiles/{platform}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": random_user_agent(),
        }
        response = requests.get(platform_url, headers)
        self.request_count += 1
        if response.status_code == 200:
            folders = json.loads(response.content)
            for folder in folders:
                folder_name = folder["name"]
                self.state.folder_hash = folder["sha"]
                folder_url = f"{platform_url}/{folder_name}"
                self._process_folder(folder_url)
        else:
            print(
                f"Failed to download {platform} folders: {response.status_code},"
                f" {response.text}"
            )

    def _process_folder(self, folder_url: str) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": random_user_agent(),
        }
        response = requests.get(folder_url, headers)
        self.request_count += 1
        if response.status_code == 200:
            files = json.loads(response.content)
            for file in files:
                self.state.file_hash = file["sha"]
                self._process_file(file["download_url"])
        else:
            print(
                f"Failed to download {folder_url} contents: {response.status_code},"
                f" {response.text}"
            )

    def _process_file(self, download_url: str) -> None:
        if self.file_in_import_state_log():
            return

        headers = {
            "Accept": "application/json",
            "User-Agent": random_user_agent(),
        }
        response = requests.get(download_url, headers)
        if response.status_code == 200:
            try:
                src_artifact = AppleDbArtifact.model_validate_json(response.content)
            except ValidationError as e:
                # this should probably just send to sentry
                logger.error(e)
                self.update_import_state_log()
                return

            if len(src_artifact.sources) > 0:
                ipsw_sources: list[IpswSource] = []
                for source in src_artifact.sources:
                    if source.link and source.type == "ipsw":
                        ipsw_sources.append(
                            IpswSource(**source.model_dump(exclude={"type", "links"}))
                        )
                if len(ipsw_sources) == 0:
                    self.update_import_state_log()
                    return

                src_dump = src_artifact.model_dump(exclude={"rc", "beta", "sources"})
                src_dump["platform"] = self.state.platform
                src_dump["sources"] = ipsw_sources
                artifact = IpswArtifact(**src_dump)
                if self.meta_db.contains(artifact.key):
                    # this only checks if we already have that id, but it doesn't ask whether they
                    # differ... this should be easy to check with pydantic, but it might help to print
                    # the diff with something like deepdiff
                    logger.warning(
                        f"{artifact.key} already added\n\told ="
                        f" {self.meta_db.artifacts[artifact.key]}\n\tnew ="
                        f" {artifact}"
                    )
                else:
                    self.meta_db.artifacts[artifact.key] = artifact

                self.update_import_state_log()
        else:
            print(
                f"Failed to download {download_url} contents:"
                f" {response.status_code}, {response.text}"
            )

    def file_in_import_state_log(self) -> bool:
        return (
            self.state.folder_hash in self.apple_db_import_state
            and self.state.file_hash
            in self.apple_db_import_state[self.state.folder_hash]
        )

    def update_import_state_log(self) -> None:
        assert self.state.folder_hash is not None
        assert self.state.file_hash is not None
        if self.state.folder_hash in self.apple_db_import_state:
            if (
                self.state.file_hash
                not in self.apple_db_import_state[self.state.folder_hash]
            ):
                self.apple_db_import_state[self.state.folder_hash].append(
                    self.state.file_hash
                )
        else:
            self.apple_db_import_state[self.state.folder_hash] = []
            self.apple_db_import_state[self.state.folder_hash].append(
                self.state.file_hash
            )


def main() -> None:
    AppleDbIspwImport().run()


if __name__ == "__main__":
    main()
