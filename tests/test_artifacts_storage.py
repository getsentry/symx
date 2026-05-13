from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage import Bucket
from pydantic import HttpUrl

from symx.artifacts.storage import ArtifactGcsPrefixStore, ArtifactStorageError, normalize_prefix
from symx.ipsw.model import IpswArtifact, IpswArtifactDb, IpswPlatform, IpswReleaseStatus, IpswSource
from symx.ota.model import OtaArtifact


@dataclass
class FakeObject:
    payload: str
    generation: int
    content_type: str | None = None


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.generation: int | None = None
        self.size: int | None = None
        self._hydrate_metadata()

    def exists(self, retry: object | None = None) -> bool:
        return self.name in self._bucket.objects

    def reload(self, retry: object | None = None) -> None:
        self._hydrate_metadata()

    def download_to_filename(self, filename: str) -> None:
        with open(filename, "w") as f:
            f.write(self._bucket.objects[self.name].payload)

    def download_as_bytes(self) -> bytes:
        return self._bucket.objects[self.name].payload.encode("utf-8")

    def upload_from_string(
        self,
        data: str,
        content_type: str | None = None,
        retry: object | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        self._upload_payload(data, content_type, if_generation_match)

    def upload_from_filename(self, filename: str, if_generation_match: int | None = None) -> None:
        with open(filename, "rb") as f:
            self._upload_payload(f.read().decode("latin1"), None, if_generation_match)

    def _upload_payload(
        self,
        data: str,
        content_type: str | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        if if_generation_match == 0 and self.name in self._bucket.objects:
            raise PreconditionFailed("exists")
        self._bucket.generation += 1
        self._bucket.objects[self.name] = FakeObject(data, self._bucket.generation, content_type)
        self._bucket.write_order.append(self.name)
        self._hydrate_metadata()

    def _hydrate_metadata(self) -> None:
        obj = self._bucket.objects.get(self.name)
        if obj is None:
            self.generation = None
            self.size = None
        else:
            self.generation = obj.generation
            self.size = len(obj.payload.encode())


class FakeBucket:
    def __init__(self, objects: dict[str, str]) -> None:
        self.generation = 100
        self.write_order: list[str] = []
        self.objects = {
            name: FakeObject(payload=payload, generation=index + 1)
            for index, (name, payload) in enumerate(objects.items())
        }

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def list_blobs(self, prefix: str):
        for name in sorted(self.objects):
            if name.startswith(prefix):
                yield FakeBlob(self, name)


def test_normalize_prefix_rejects_empty_prefix() -> None:
    try:
        normalize_prefix("/")
    except ArtifactStorageError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("expected ArtifactStorageError")


def test_bootstrap_writes_normalized_objects_create_only() -> None:
    bucket = FakeBucket(
        {
            "ipsw_meta.json": _ipsw_db_json(),
            "ota_image_meta.json": _ota_meta_json(),
        }
    )
    store = ArtifactGcsPrefixStore(
        cast(Bucket, cast(Any, bucket)),
        "experiments/meta-v2/test-run",
        "gs://apple_symbols",
    )

    result = store.bootstrap(max_workers=1)

    assert result.manifest.artifact_count == 2
    assert result.manifest.detail_count == 2
    assert result.manifest.parity_ok
    assert result.written_object_count == 6
    assert "ipsw_meta.json" in bucket.objects
    assert "ota_image_meta.json" in bucket.objects
    assert result.manifest.manifest_path in bucket.objects
    assert result.manifest.parity_report_path in bucket.objects
    assert bucket.write_order[-1] == result.manifest.manifest_path
    assert all(name.startswith("experiments/meta-v2/test-run/") for name in result.sample_written_objects)


def test_write_local_snapshot_from_v2_reads_bootstrapped_v2_objects(tmp_path: Path) -> None:
    bucket = FakeBucket(
        {
            "ipsw_meta.json": _ipsw_db_json(),
            "ota_image_meta.json": _ota_meta_json(),
        }
    )
    store = ArtifactGcsPrefixStore(
        cast(Bucket, cast(Any, bucket)),
        "experiments/meta-v2/test-run",
        "gs://apple_symbols",
    )
    store.bootstrap(max_workers=1)

    output_path = tmp_path / "snapshot.db"
    result = store.write_local_snapshot_from_v2(output_path, max_workers=1)

    assert result.output_path == str(output_path)
    assert result.artifact_count == 2
    assert result.snapshot_counts.artifacts == 2
    assert result.snapshot_counts.ipsw_details == 1
    assert result.snapshot_counts.ota_details == 1
    assert output_path.exists()


def test_write_snapshot_view_writes_one_snapshot_object() -> None:
    bucket = FakeBucket(
        {
            "ipsw_meta.json": _ipsw_db_json(),
            "ota_image_meta.json": _ota_meta_json(),
        }
    )
    store = ArtifactGcsPrefixStore(
        cast(Bucket, cast(Any, bucket)),
        "experiments/meta-v2/test-run",
        "gs://apple_symbols",
    )

    result = store.write_snapshot_view()

    assert result.written_object_count == 1
    assert result.snapshot_counts.artifacts == 2
    assert result.snapshot_db_path == "experiments/meta-v2/test-run/views/snapshot.db"
    assert result.snapshot_db_path in bucket.objects
    assert bucket.write_order == [result.snapshot_db_path]


def test_bootstrap_refuses_existing_experiment_objects() -> None:
    bucket = FakeBucket(
        {
            "ipsw_meta.json": _ipsw_db_json(),
            "ota_image_meta.json": _ota_meta_json(),
            "experiments/meta-v2/test-run/reports/parity.json": "{}",
        }
    )
    store = ArtifactGcsPrefixStore(
        cast(Bucket, cast(Any, bucket)),
        "experiments/meta-v2/test-run",
        "gs://apple_symbols",
    )

    try:
        store.bootstrap(max_workers=1)
    except ArtifactStorageError as exc:
        assert "Refusing to overwrite" in str(exc)
    else:
        raise AssertionError("expected ArtifactStorageError")


def _ipsw_db_json() -> str:
    artifact = IpswArtifact(
        platform=IpswPlatform.IOS,
        version="18.2",
        build="22C152",
        released=date.today(),
        release_status=IpswReleaseStatus.RELEASE,
        sources=[
            IpswSource(
                devices=["iPhone14,7"],
                link=HttpUrl("https://example.com/test.ipsw"),
            )
        ],
    )
    return IpswArtifactDb(artifacts={artifact.key: artifact}).model_dump_json()


def _ota_meta_json() -> str:
    ota = OtaArtifact(
        build="22C152",
        description=["iOS 18.2"],
        version="18.2",
        platform="ios",
        id="a" * 40,
        url=f"https://updates.apple.com/{'a' * 40}.zip",
        download_path=None,
        devices=["iPhone14,7"],
        hash="def456",
        hash_algorithm="SHA-1",
    )
    return json.dumps({"ota-key": ota.model_dump(mode="json")})
