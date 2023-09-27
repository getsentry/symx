import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
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

IMPORT_STATE_JSON = "appledb_import_state.json"

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", None)


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
    def size_must_be_a_positive_int(cls, v: int) -> int:
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
        if self.rc is True:
            return IpswReleaseStatus.RELEASE_CANDIDATE
        elif self.beta is True:
            return IpswReleaseStatus.BETA

        return IpswReleaseStatus.RELEASE


def ipsw_filename_from_url(url: str) -> str:
    parsed_url = urlparse(url)
    path = parsed_url.path
    return path.split("/")[-1][:-5]


API_CONTENTS_URL = "https://api.github.com/repos/littlebyteorg/appledb/contents/"


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


@dataclass
class AppleDbIpswImportState:
    platform: str | None = None
    folder_hash: str | None = None
    file_hash: str | None = None


def _folder_sort_key(item: dict[str, str]) -> int:
    folder_name: str = item["name"]
    x_idx = folder_name.find("x")
    try:
        sort_key = int(folder_name[0:x_idx])
    except ValueError:
        sort_key = -1

    return sort_key


def _file_sort_key(item: dict[str, str]) -> str:
    file_name: str = item["name"]
    return file_name


class GithubAPIResponse(BaseModel):
    message: str
    documentation_url: HttpUrl


class AppleDbIpswImport:
    def __init__(self, processing_dir: Path) -> None:
        self._processing_dir = processing_dir
        self._load_appledb_indexed()
        self._load_meta_db()
        self.api_request_count = 0
        self.file_request_count = 0
        self.processed_file_count = 0
        self.already_imported_count = 0
        self.artifact_wo_sources_count = 0
        self.state = AppleDbIpswImportState()
        self.new_artifacts: list[IpswArtifact] = []

    def run(self) -> None:
        try:
            platforms = list(IpswPlatform)

            # ignore IPod IPSWs when syncing
            platforms.remove(IpswPlatform.IPODOS)

            # there is no particular reason to iterate by any order through the platforms so let's shuffle (#rate-limit)
            random.shuffle(platforms)
            for platform in platforms:
                self._process_platform(platform)
        except Exception as e:
            sentry_sdk.capture_exception(e)
        finally:
            logger.info(f"Number of github API requests = {self.api_request_count}")
            logger.info(f"Number of github file requests = {self.file_request_count}")
            logger.info(f"Number of processed files = {self.processed_file_count}")
            logger.info(
                f"Number of already imported files = {self.already_imported_count}"
            )
            logger.info(
                f"Number of artifacts w/o sources = {self.artifact_wo_sources_count}"
            )

            self._store_appledb_indexed()

    def _store_ipsw_meta(self) -> None:
        with open(self._processing_dir / ARTIFACTS_META_JSON, "w") as fp:
            fp.write(self.meta_db.model_dump_json())

    def _store_appledb_indexed(self) -> None:
        with open(self._processing_dir / IMPORT_STATE_JSON, "w") as fp:
            json.dump(self.apple_db_import_state, fp)

    def _load_meta_db(self) -> None:
        try:
            fp = open(self._processing_dir / ARTIFACTS_META_JSON)
        except IOError:
            self.meta_db = IpswArtifactDb()
        else:
            with fp:
                self.meta_db = IpswArtifactDb.model_validate_json(fp.read())

    def _load_appledb_indexed(self) -> None:
        try:
            fp = open(self._processing_dir / IMPORT_STATE_JSON)
        except IOError:
            self.apple_db_import_state: dict[str, list[str]] = {}
        else:
            with fp:
                self.apple_db_import_state = json.load(fp)

    def _github_api_request(self, url: str) -> bytes | None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": random_user_agent(),
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"

        response = requests.get(url, headers)
        self.api_request_count += 1
        if response.status_code != 200:
            github_response = GithubAPIResponse.model_validate_json(response.text)
            # we are not interested in rate-limit error notifications only log a warning
            if (
                response.status_code == 403
                and "API rate limit exceeded" in github_response.message
            ):
                logger.warning(github_response.message)
            else:
                logger.error(
                    f"Failed github API GET-request: {response.status_code},"
                    f" {github_response}"
                )
            return None

        return response.content

    def _process_platform(self, platform: str) -> None:
        self.state.platform = platform
        sentry_sdk.set_tag("ipsw.import.appledb.platform", platform)
        platform_url = f"{API_CONTENTS_URL}osFiles/{platform}"
        response = self._github_api_request(platform_url)
        if not response:
            return

        platform_items: list[dict[str, Any]] = json.loads(response)

        # iterate platform_items starting with the latest releases
        for item in sorted(platform_items, key=_folder_sort_key, reverse=True):
            if item["type"] == "dir":
                folder_name = item["name"]
                self.state.folder_hash = item["sha"]
                sentry_sdk.set_tag(
                    "ipsw.import.appledb.folder_hash", self.state.folder_hash
                )
                sentry_sdk.set_tag("ipsw.import.appledb.folder_name", folder_name)
                folder_url = f"{platform_url}/{folder_name}"
                self._process_folder(folder_url)
            elif item["type"] == "file":
                self.state.file_hash = item["sha"]
                download_url = item["download_url"]
                sentry_sdk.set_tag("ipsw.import.appledb.download_url", download_url)
                sentry_sdk.set_tag(
                    "ipsw.import.appledb.file_hash", self.state.file_hash
                )
                self._process_file(download_url)

    def _process_folder(self, folder_url: str) -> None:
        response = self._github_api_request(folder_url)
        if not response:
            return

        files = json.loads(response)
        for file in sorted(files, key=_file_sort_key, reverse=True):
            self.state.file_hash = file["sha"]
            download_url = file["download_url"]
            sentry_sdk.set_tag("ipsw.import.appledb.download_url", download_url)
            sentry_sdk.set_tag("ipsw.import.appledb.file_hash", self.state.file_hash)
            self._process_file(download_url)

    def _process_file(self, download_url: str) -> None:
        logger.info(
            f"About to process {download_url} (file-hash: {self.state.file_hash}) in"
            f" {self.state.platform} folder {self.state.folder_hash}"
        )
        self.processed_file_count += 1
        if self.file_in_import_state_log():
            self.already_imported_count += 1
            logger.info(f"{download_url} already processed continue with next")
            return

        headers = {
            "Accept": "application/json",
            "User-Agent": random_user_agent(),
        }
        response = requests.get(download_url, headers)
        self.file_request_count += 1
        if response.status_code != 200:
            logger.error(
                "Failed to download file contents:"
                f" {response.status_code}, {response.text}"
            )
            return

        try:
            src_artifact = AppleDbArtifact.model_validate_json(response.content)
        except ValidationError as e:
            sentry_sdk.capture_exception(e)
            self.update_import_state_log()
            return

        # either the artifact has no sources at all...
        if len(src_artifact.sources) == 0:
            self.update_import_state_log()
            self.artifact_wo_sources_count += 1
            logger.warning("IPSW artifact has no sources and won't be imported")
            return

        ipsw_sources: list[IpswSource] = []
        for source in src_artifact.sources:
            if source.link and source.type == "ipsw":
                ipsw_sources.append(
                    IpswSource(**source.model_dump(exclude={"type", "links"}))
                )
        # ...or it has no usable sources (e.g. URLs that are no longer active, non-IPSW source, etc.)
        if len(ipsw_sources) == 0:
            self.update_import_state_log()
            self.artifact_wo_sources_count += 1
            logger.warning("IPSW artifact has no usable sources and won't be imported")
            return

        src_dump = src_artifact.model_dump(exclude={"rc", "beta", "sources"})
        src_dump["platform"] = self.state.platform
        src_dump["sources"] = ipsw_sources
        artifact = IpswArtifact(**src_dump)
        if self.meta_db.contains(artifact.key):
            # this only checks if we already have that id, but it doesn't ask whether they
            # differ... this should be easy to check with pydantic, but it might help to log
            # the diff with something like deepdiff
            logger.warning(
                f"{artifact.key} already added\n\told ="
                f" {self.meta_db.get(artifact.key)}\n\tnew ="
                f" {artifact}"
            )
        else:
            self.new_artifacts.append(artifact)

        self.update_import_state_log()

    def file_in_import_state_log(self) -> bool:
        return (
            self.state.folder_hash in self.apple_db_import_state
            and self.state.file_hash
            in self.apple_db_import_state[self.state.folder_hash]
        )

    def update_import_state_log(self) -> None:
        assert self.state.file_hash is not None
        folder_hash = (
            self.state.folder_hash
            if self.state.folder_hash is not None
            else self.state.file_hash
        )

        if folder_hash in self.apple_db_import_state:
            if self.state.file_hash not in self.apple_db_import_state[folder_hash]:
                self.apple_db_import_state[folder_hash].append(self.state.file_hash)
        else:
            self.apple_db_import_state[folder_hash] = []
            self.apple_db_import_state[folder_hash].append(self.state.file_hash)
