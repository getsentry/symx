from __future__ import annotations

from symx.artifacts.json_store import JsonMetadataKind, simulate_json_state_update, upload_legacy_json_copies
from tests.test_artifacts_storage import FakeBucket, _ipsw_db_json, _ota_meta_json


def test_upload_legacy_json_copies_create_only(monkeypatch) -> None:
    fake_bucket = FakeBucket(
        {
            "ipsw_meta.json": _ipsw_db_json(),
            "ota_image_meta.json": _ota_meta_json(),
        }
    )
    _patch_client(monkeypatch, fake_bucket)

    result = upload_legacy_json_copies("gs://apple_symbols/", "experiments/meta-json/test")

    assert result.uploaded_objects == [
        "experiments/meta-json/test/ipsw_meta.json",
        "experiments/meta-json/test/ota_image_meta.json",
    ]
    assert result.uploaded_objects[0] in fake_bucket.objects
    assert result.uploaded_objects[1] in fake_bucket.objects


def test_simulate_json_state_update_updates_ipsw_object(monkeypatch) -> None:
    fake_bucket = FakeBucket({"experiments/meta-json/test/ipsw_meta.json": _ipsw_db_json()})
    _patch_client(monkeypatch, fake_bucket)

    result = simulate_json_state_update(
        "gs://apple_symbols/",
        "experiments/meta-json/test/ipsw_meta.json",
        JsonMetadataKind.IPSW,
    )

    assert result.kind == JsonMetadataKind.IPSW
    assert result.previous_state == "indexed"
    assert result.new_state == "ignored"
    assert result.generation_after > result.generation_before


def test_simulate_json_state_update_updates_ota_object(monkeypatch) -> None:
    fake_bucket = FakeBucket({"experiments/meta-json/test/ota_image_meta.json": _ota_meta_json()})
    _patch_client(monkeypatch, fake_bucket)

    result = simulate_json_state_update(
        "gs://apple_symbols/",
        "experiments/meta-json/test/ota_image_meta.json",
        JsonMetadataKind.OTA,
    )

    assert result.kind == JsonMetadataKind.OTA
    assert result.previous_state == "indexed"
    assert result.new_state == "ignored"
    assert result.generation_after > result.generation_before


def _patch_client(monkeypatch, fake_bucket: FakeBucket) -> None:
    class FakeClient:
        def __init__(self, project=None) -> None:
            pass

        def bucket(self, bucket_name: str) -> FakeBucket:
            return fake_bucket

    monkeypatch.setattr("symx.artifacts.json_store.Client", FakeClient)
