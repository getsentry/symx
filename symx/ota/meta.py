"""OTA meta-data parsing, retrieval from Apple, and merge logic."""

import json
import logging
import subprocess

import sentry_sdk
import sentry_sdk.metrics

from symx.common import ArtifactProcessingState
from symx.ota.common import (
    PLATFORMS,
    OtaArtifact,
    OtaMetaData,
)

logger = logging.getLogger(__name__)


def parse_download_meta_output(
    platform: str,
    result: subprocess.CompletedProcess[bytes],
    meta_data: OtaMetaData,
    beta: bool,
) -> None:
    if result.returncode != 0:
        ipsw_stderr = result.stderr.decode("utf-8")
        # We regularly get 403 errors on the apple endpoint. These seem to be intermittent
        # availability issues and do not warrant error notification noise.
        if "api returned status: 403 Forbidden" not in ipsw_stderr:
            logger.error("Download OTA meta failed for %s%s", platform, " (beta)" if beta else "")
    else:
        platform_meta = json.loads(result.stdout)
        for meta_item in platform_meta:
            url = meta_item["url"]
            sentry_sdk.set_tag("artifact.url", url)
            zip_id_start_idx = url.rfind("/") + 1
            zip_id = url[zip_id_start_idx:-4]
            # zip ids are either SHA1 (40 hex digits) or SHA256 (64 hex digits)
            if len(zip_id) not in (40, 64):
                logger.error("Parsing download meta: unexpected url-format")

            if "description" in meta_item:
                desc = [meta_item["description"]]
            else:
                desc = []

            if beta:
                # betas can have the same zip-id as later releases, often with the same contents
                # they only differ by the build. we need to tag them in the key, and we should add
                # a state INDEXED_DUPLICATE as to not process them twice.
                key = zip_id + "_beta"
            else:
                key = zip_id

            meta_data[key] = OtaArtifact(
                id=zip_id,
                build=meta_item["build"],
                description=desc,
                version=meta_item["version"],
                platform=platform,
                url=url,
                devices=meta_item.get("devices", []),
                download_path=None,
                hash=meta_item["hash"],
                hash_algorithm=meta_item["hash_algorithm"],
            )


def retrieve_current_meta() -> OtaMetaData:
    meta: OtaMetaData = {}
    for platform in PLATFORMS:
        with sentry_sdk.start_span(op="subprocess.ipsw_download_meta", name=f"Fetch OTA meta for {platform}") as span:
            span.set_data("platform", platform)
            logger.info("Downloading OTA meta for %s", platform)
            cmd = [
                "ipsw",
                "download",
                "ota",
                "--platform",
                platform,
                "--urls",
                "--json",
            ]

            parse_download_meta_output(platform, subprocess.run(cmd, capture_output=True), meta, False)

            beta_cmd = cmd.copy()
            beta_cmd.append("--beta")
            parse_download_meta_output(platform, subprocess.run(beta_cmd, capture_output=True), meta, True)

    sentry_sdk.metrics.distribution("ota.meta_sync.total_artifacts", len(meta))
    return meta


def merge_lists(a: list[str] | None, b: list[str] | None) -> list[str]:
    if a is None:
        a = []
    if b is None:
        b = []
    return list(set(a + b))


def generate_duplicate_key_from(ours: OtaMetaData, their_key: str) -> str:
    duplicate_num = 1
    key_candidate = f"{their_key}_duplicate_{duplicate_num}"

    while key_candidate in ours.keys():
        duplicate_num += 1
        key_candidate = f"{their_key}_duplicate_{duplicate_num}"

    return key_candidate


def merge_meta_data(ours: OtaMetaData, theirs: OtaMetaData) -> None:
    """
    This function is at the core of the whole thing:
        - What meta-data does Apple consider as identity?
        - What is sufficient "identity" to correctly map to the symbols store?
        - Which merge/duplication strategy should we apply if items turn out to be the same?
        - How to migrate if identities (or our knowledge of identities) change?
    The answers to these questions are encoded in this function.
    :param ours: The meta-data of all OTA artifacts in our store.
    :param theirs: The meta-data of all OTA artifacts currently provided by Apple.
    :return: None
    """
    for their_key, their_item in theirs.items():
        if their_key in ours.keys():
            # we already have that id in out meta-store
            our_item = ours[their_key]

            # merge data that can change over time but has no effect on the identity of the artifact
            ours[their_key].description = merge_lists(our_item.description, their_item.description)
            ours[their_key].devices = merge_lists(our_item.devices, their_item.devices)

            # If we have
            # - a differing build or
            # - a differing url
            # but all other values that contribute to identity are the same then we have a duplicate that requires a
            # corresponding duplicate key.
            #
            # Another option would be to merge the builds and have them as an array of a single meta-data item, but that
            # wouldn't really help with the identity back-refs from the store (or any other identity resolution that
            # goes beyond that). For our purposes we should treat this as a separate artifact where we append to the key
            # so that the key-prefix is maintained and set the processing state to INDEXED_DUPLICATE.
            if (
                their_item.build != our_item.build
                and their_item.version == our_item.version
                and their_item.platform == our_item.platform
                and their_item.url == our_item.url
                and their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
            ) or (
                their_item.url != our_item.url
                and their_item.build == our_item.build
                and their_item.version == our_item.version
                and their_item.platform == our_item.platform
                and their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
            ):
                duplicate_key = generate_duplicate_key_from(ours, their_key)
                ours[duplicate_key] = their_item
                ours[duplicate_key].processing_state = ArtifactProcessingState.INDEXED_DUPLICATE
                continue

            # if any of the remaining identity-contributing values differ at this point then our identity matching is
            # still incomplete.
            if not (
                their_item.build == our_item.build
                and their_item.version == our_item.version
                and their_item.platform == our_item.platform
                and their_item.url == our_item.url
                and their_item.hash == our_item.hash
                and their_item.hash_algorithm == our_item.hash_algorithm
            ):
                raise RuntimeError(f"Matching keys with different value:\n\tlocal: {our_item}\n\tapple: {their_item}")
        else:
            # it is a new key, store their item in our store
            ours[their_key] = their_item

            # identify and mark beta <-> normal release duplicates
            for _, our_v in ours.items():
                if (
                    their_item.hash == our_v.hash
                    and their_item.hash_algorithm == our_v.hash_algorithm
                    and their_item.platform == our_v.platform
                    and their_item.version == our_v.version
                    and their_item.build != our_v.build
                ):
                    ours[their_key].processing_state = ArtifactProcessingState.INDEXED_DUPLICATE
                    break
