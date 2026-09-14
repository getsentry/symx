"""
Tests for OTA metadata merge logic.

merge_meta_data() is the core function that reconciles our stored metadata with
fresh data from Apple. It handles:
- Merging device/description lists
- Detecting content aliases with changed build/version/URL metadata
- Detecting beta/release duplicates (same hash, different build)
- Raising errors on identity mismatches
"""

from datetime import UTC, datetime

import pytest

from symx.model import ArtifactProcessingState
from symx.ota.model import OtaArtifact, OtaDelivery, OtaMetaData
from symx.ota.meta import generate_duplicate_key_from, merge_meta_data


def make_ota_artifact(
    id: str = "387534500408f0c0867b48bef124a1e581b12ed0",
    build: str = "21C66",
    version: str = "17.2.1",
    platform: str = "ios",
    url: str | None = None,
    hash: str = "67af066e7cb5e9548ec57d6eff295c20df1758b6",
    hash_algorithm: str = "SHA-1",
    description: list[str] | None = None,
    devices: list[str] | None = None,
    download_path: str | None = None,
    processing_state: ArtifactProcessingState = ArtifactProcessingState.INDEXED,
    release_type: str | None = None,
    asset_type: str | None = None,
    delivery: OtaDelivery | None = None,
    prerequisite_build: str | None = None,
    prerequisite_version: str | None = None,
    supported_models: list[str] | None = None,
    last_modified: datetime | None = None,
) -> OtaArtifact:
    if url is None:
        url = f"https://updates.cdn-apple.com/2023FallFCS/patches/{id}.zip"
    return OtaArtifact(
        id=id,
        build=build,
        version=version,
        platform=platform,
        url=url,
        hash=hash,
        hash_algorithm=hash_algorithm,
        description=description or [],
        devices=devices or [],
        download_path=download_path,
        processing_state=processing_state,
        release_type=release_type,
        asset_type=asset_type,
        delivery=delivery,
        prerequisite_build=prerequisite_build,
        prerequisite_version=prerequisite_version,
        supported_models=supported_models or [],
        last_run=0,
        last_modified=last_modified,
    )


def test_generate_duplicate_key_increments_until_unique() -> None:
    their_key = "abc123"
    artifact = make_ota_artifact(id=their_key)
    meta_store: OtaMetaData = {
        their_key: artifact,
        f"{their_key}_duplicate_1": artifact,
        f"{their_key}_duplicate_2": artifact,
    }

    assert generate_duplicate_key_from(meta_store, their_key) == f"{their_key}_duplicate_3"


def test_merge_deduplicates_description_and_device_lists() -> None:
    ours: OtaMetaData = {
        "key1": make_ota_artifact(
            id="key1",
            description=["desc1", "desc2"],
            devices=["iPhone11,2", "iPhone11,6"],
        )
    }
    theirs: OtaMetaData = {
        "key1": make_ota_artifact(
            id="key1",
            description=["desc2", "desc3"],
            devices=["iPhone11,6", "iPhone12,1"],
        )
    }

    merge_meta_data(ours, theirs)

    assert set(ours["key1"].description) == {"desc1", "desc2", "desc3"}
    assert set(ours["key1"].devices) == {"iPhone11,2", "iPhone11,6", "iPhone12,1"}


def test_merge_hydrates_classification_metadata_without_changing_processing_state() -> None:
    previous_update = datetime(2026, 9, 13, 12, tzinfo=UTC)
    ours: OtaMetaData = {
        "key1": make_ota_artifact(
            id="key1",
            processing_state=ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
            download_path="mirror/ota/ios/key1.zip",
            last_modified=previous_update,
        )
    }
    theirs: OtaMetaData = {
        "key1": make_ota_artifact(
            id="key1",
            release_type="Darwin Recovery",
            asset_type="com.apple.MobileAsset.RecoveryOSUpdate",
            delivery="delta",
            prerequisite_build="20A99",
            prerequisite_version="16.6",
            supported_models=["D27AP"],
            last_modified=datetime(2026, 9, 14, 12, tzinfo=UTC),
        )
    }

    merge_meta_data(ours, theirs)

    artifact = ours["key1"]
    assert artifact.release_type == "Darwin Recovery"
    assert artifact.asset_type == "com.apple.MobileAsset.RecoveryOSUpdate"
    assert artifact.delivery == "delta"
    assert artifact.prerequisite_build == "20A99"
    assert artifact.prerequisite_version == "16.6"
    assert artifact.supported_models == ["D27AP"]
    assert artifact.processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
    assert artifact.download_path == "mirror/ota/ios/key1.zip"
    assert artifact.last_modified == previous_update


def test_merge_preserves_our_processing_state_and_download_path() -> None:
    """Merging from Apple should never regress our workflow progress."""
    ours: OtaMetaData = {
        "key1": make_ota_artifact(
            id="key1",
            processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED,
            download_path="mirror/ota/ios/key1.zip",
        )
    }
    theirs: OtaMetaData = {
        "key1": make_ota_artifact(
            id="key1",
            processing_state=ArtifactProcessingState.INDEXED,
            download_path=None,
        )
    }

    merge_meta_data(ours, theirs)

    assert ours["key1"].processing_state == ArtifactProcessingState.SYMBOLS_EXTRACTED
    assert ours["key1"].download_path == "mirror/ota/ios/key1.zip"


def test_different_build_same_hash_creates_duplicate() -> None:
    """Same file (same hash/URL) with different build number = duplicate."""
    ours: OtaMetaData = {
        "key1": make_ota_artifact(id="key1", build="21C66", url="https://example.com/key1.zip", hash="abc123")
    }
    theirs: OtaMetaData = {
        "key1": make_ota_artifact(id="key1", build="21C67", url="https://example.com/key1.zip", hash="abc123")
    }

    merge_meta_data(ours, theirs)

    assert len(ours) == 2
    assert ours["key1"].build == "21C66"
    assert ours["key1_duplicate_1"].build == "21C67"
    assert ours["key1_duplicate_1"].processing_state == ArtifactProcessingState.INDEXED_DUPLICATE


def test_different_url_same_hash_creates_duplicate() -> None:
    """Same file (same hash) from different URL = duplicate."""
    ours: OtaMetaData = {"key1": make_ota_artifact(id="key1", url="https://example.com/old/key1.zip", hash="abc123")}
    theirs: OtaMetaData = {"key1": make_ota_artifact(id="key1", url="https://example.com/new/key1.zip", hash="abc123")}

    merge_meta_data(ours, theirs)

    assert len(ours) == 2
    assert ours["key1_duplicate_1"].processing_state == ArtifactProcessingState.INDEXED_DUPLICATE


def test_beta_and_release_same_hash_marks_duplicate() -> None:
    """Beta release with same hash as GA release = duplicate (common scenario)."""
    ours: OtaMetaData = {"abc123": make_ota_artifact(id="abc123", build="21A100", hash="samehash")}
    theirs: OtaMetaData = {"abc123_beta": make_ota_artifact(id="abc123", build="21A99", hash="samehash")}

    merge_meta_data(ours, theirs)

    assert ours["abc123_beta"].processing_state == ArtifactProcessingState.INDEXED_DUPLICATE


def test_new_artifact_without_matching_hash_stays_indexed() -> None:
    """A genuinely new artifact should stay INDEXED for processing."""
    ours: OtaMetaData = {"existing": make_ota_artifact(id="existing", hash="hash_a")}
    theirs: OtaMetaData = {"new_key": make_ota_artifact(id="new_key", hash="different_hash")}

    merge_meta_data(ours, theirs)

    assert ours["new_key"].processing_state == ArtifactProcessingState.INDEXED


def test_same_content_with_different_version_creates_one_stable_duplicate() -> None:
    ours: OtaMetaData = {
        "key1": make_ota_artifact(
            id="key1",
            version="9.3.5",
            build="13G36",
            processing_state=ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED,
        )
    }
    their_item = make_ota_artifact(
        id="key1",
        version="7.1.2",
        build="11D257",
        delivery="delta",
        prerequisite_build="11D167",
    )
    theirs: OtaMetaData = {"key1": their_item}

    merge_meta_data(ours, theirs)
    merge_meta_data(ours, theirs)

    assert len(ours) == 2
    assert ours["key1"].version == "9.3.5"
    assert ours["key1"].processing_state == ArtifactProcessingState.SYMBOL_EXTRACTION_FAILED
    duplicate = ours["key1_duplicate_1"]
    assert duplicate.version == "7.1.2"
    assert duplicate.build == "11D257"
    assert duplicate.delivery == "delta"
    assert duplicate.prerequisite_build == "11D167"
    assert duplicate.processing_state == ArtifactProcessingState.INDEXED_DUPLICATE


def test_different_platform_raises_error() -> None:
    ours: OtaMetaData = {"key1": make_ota_artifact(id="key1", platform="ios")}
    theirs: OtaMetaData = {"key1": make_ota_artifact(id="key1", platform="watchos")}

    with pytest.raises(RuntimeError, match="Matching keys with different value"):
        merge_meta_data(ours, theirs)


def test_different_hash_raises_error() -> None:
    ours: OtaMetaData = {"key1": make_ota_artifact(id="key1", hash="hash1")}
    theirs: OtaMetaData = {"key1": make_ota_artifact(id="key1", hash="hash2")}

    with pytest.raises(RuntimeError, match="Matching keys with different value"):
        merge_meta_data(ours, theirs)


def test_merge_is_idempotent() -> None:
    ours: OtaMetaData = {"key1": make_ota_artifact(id="key1")}
    theirs: OtaMetaData = {"key1": make_ota_artifact(id="key1")}

    merge_meta_data(ours, theirs)
    merge_meta_data(ours, theirs)

    assert len(ours) == 1
