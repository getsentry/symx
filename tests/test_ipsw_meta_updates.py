from datetime import date
from typing import cast

from google.cloud.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket
from pydantic import HttpUrl

from symx.ipsw.model import IpswArtifact, IpswArtifactDb, IpswPlatform, IpswReleaseStatus, IpswSource
from symx.ipsw.runners import import_meta_from_appledb
from symx.ipsw.storage.gcs import IpswGcsStorage, IpswMetaSnapshot
from symx.model import ArtifactProcessingState


class _FakeBlob:
    def __init__(
        self,
        *,
        fail_upload: bool = False,
        generation: int | None = None,
        exists_result: bool = True,
        reload_generation: int | None = None,
    ) -> None:
        self.fail_upload = fail_upload
        self.generation = generation
        self.exists_result = exists_result
        self.reload_generation = reload_generation
        self.downloads: list[str] = []
        self.reload_count = 0
        self.uploads: list[tuple[str, int]] = []

    def exists(self) -> bool:
        return self.exists_result

    def download_to_filename(self, filename: str) -> None:
        self.downloads.append(filename)
        with open(filename, "w") as fp:
            fp.write(IpswArtifactDb().model_dump_json())

    def reload(self) -> None:
        self.reload_count += 1
        self.generation = self.reload_generation

    def upload_from_string(self, payload: str, if_generation_match: int) -> None:
        self.uploads.append((payload, if_generation_match))
        if self.fail_upload:
            self.fail_upload = False
            raise PreconditionFailed("stale generation")


class _FakeBucket:
    def __init__(self, blob: _FakeBlob) -> None:
        self.blob_calls: list[str] = []
        self._blob = blob

    def blob(self, name: str) -> _FakeBlob:
        self.blob_calls.append(name)
        return self._blob


class _LoadOnlyIpswGcsStorage(IpswGcsStorage):
    def __init__(self, local_dir, blob: _FakeBlob) -> None:
        self.local_dir = local_dir
        self.local_artifacts_meta = local_dir / "ipsw_meta.json"
        self.bucket = cast(Bucket, _FakeBucket(blob))


class _RefreshableIpswGcsStorage(IpswGcsStorage):
    def __init__(
        self, snapshots: list[tuple[_FakeBlob, IpswArtifactDb, int]], upload_blob: _FakeBlob | None = None
    ) -> None:
        self._snapshots = snapshots
        self.refresh_count = 0
        self.bucket = cast(Bucket, _FakeBucket(upload_blob or _FakeBlob()))

    def refresh_artifacts_db(self) -> tuple[Blob, IpswArtifactDb, int]:
        if not self._snapshots:
            raise AssertionError("Unexpected meta-data refresh")
        snapshot = self._snapshots[min(self.refresh_count, len(self._snapshots) - 1)]
        self.refresh_count += 1
        blob, meta_db, generation = snapshot
        return cast(Blob, blob), meta_db, generation


def _artifact(
    version: str, build: str, state: ArtifactProcessingState = ArtifactProcessingState.INDEXED
) -> IpswArtifact:
    return IpswArtifact(
        platform=IpswPlatform.IOS,
        version=version,
        build=build,
        released=date(2025, 1, 1),
        release_status=IpswReleaseStatus.RELEASE,
        sources=[
            IpswSource(
                devices=["iPhone15,2"],
                link=HttpUrl(f"https://updates.cdn-apple.com/iOS/iPhone_{version}_{build}_Restore.ipsw"),
                processing_state=state,
            )
        ],
    )


def test_load_artifacts_meta_returns_zero_only_for_observed_absence(tmp_path) -> None:
    stale_local_meta = tmp_path / "ipsw_meta.json"
    stale_local_meta.write_text(IpswArtifactDb().model_dump_json())
    blob = _FakeBlob(exists_result=False)
    storage = _LoadOnlyIpswGcsStorage(tmp_path, blob)

    loaded_blob, generation = storage.load_artifacts_meta()

    assert loaded_blob is blob
    assert generation == 0
    assert not stale_local_meta.exists()
    assert blob.downloads == []


def test_load_artifacts_meta_reloads_unknown_existing_generation(tmp_path) -> None:
    blob = _FakeBlob(exists_result=True, generation=None, reload_generation=321)
    storage = _LoadOnlyIpswGcsStorage(tmp_path, blob)

    loaded_blob, generation = storage.load_artifacts_meta()

    assert loaded_blob is blob
    assert generation == 321
    assert blob.reload_count == 1
    assert blob.downloads == [str(tmp_path / "ipsw_meta.json")]


def test_update_meta_items_uploads_batch_from_base_without_refreshing() -> None:
    base = IpswArtifactDb(artifacts={"existing": _artifact("18.0", "22A100")})
    new_artifacts = [_artifact("18.1", "22B100"), _artifact("18.2", "22C100")]
    blob = _FakeBlob()
    storage = _RefreshableIpswGcsStorage([], upload_blob=blob)

    updated = storage.update_meta_items(new_artifacts, base_snapshot=IpswMetaSnapshot(base, 123))

    assert storage.refresh_count == 0
    assert len(blob.uploads) == 1
    payload, if_generation_match = blob.uploads[0]
    assert if_generation_match == 123
    uploaded_db = IpswArtifactDb.model_validate_json(payload)
    assert set(uploaded_db.artifacts) == {"existing", "iOS_18.1_22B100", "iOS_18.2_22C100"}
    assert set(updated.artifacts) == set(uploaded_db.artifacts)
    assert set(base.artifacts) == {"existing"}


def test_update_meta_items_refreshes_and_replays_batch_after_generation_conflict() -> None:
    base = IpswArtifactDb(artifacts={"existing": _artifact("18.0", "22A100")})
    concurrent = _artifact("18.0.1", "22A101", ArtifactProcessingState.MIRRORED)
    latest = IpswArtifactDb(artifacts={"existing": _artifact("18.0", "22A100"), concurrent.key: concurrent})
    new_artifacts = [_artifact("18.1", "22B100"), _artifact("18.2", "22C100")]
    first_blob = _FakeBlob(fail_upload=True)
    retry_blob = _FakeBlob()
    storage = _RefreshableIpswGcsStorage([(retry_blob, latest, 124)], upload_blob=first_blob)

    updated = storage.update_meta_items(new_artifacts, base_snapshot=IpswMetaSnapshot(base, 123))

    assert storage.refresh_count == 1
    assert first_blob.uploads[0][1] == 123
    assert len(retry_blob.uploads) == 1
    payload, if_generation_match = retry_blob.uploads[0]
    assert if_generation_match == 124
    uploaded_db = IpswArtifactDb.model_validate_json(payload)
    assert set(uploaded_db.artifacts) == {"existing", concurrent.key, "iOS_18.1_22B100", "iOS_18.2_22C100"}
    assert uploaded_db.artifacts[concurrent.key].sources[0].processing_state == ArtifactProcessingState.MIRRORED
    assert set(updated.artifacts) == set(uploaded_db.artifacts)


def test_import_meta_from_appledb_reuses_initial_generation_for_batch_upsert(tmp_path, monkeypatch) -> None:
    class FakeImporter:
        instances: list["FakeImporter"] = []

        def __init__(self, processing_dir) -> None:
            self.processing_dir = processing_dir
            self.meta_db = IpswArtifactDb(artifacts={"existing": _artifact("18.0", "22A100")})
            self.new_artifacts = [_artifact("18.1", "22B100"), _artifact("18.2", "22C100")]
            self.run_called = False
            self.instances.append(self)

        def run(self) -> None:
            self.run_called = True

    class FakeStorage:
        def __init__(self) -> None:
            self.local_dir = tmp_path
            self.load_count = 0
            self.update_calls: list[tuple[list[IpswArtifact], IpswMetaSnapshot | None]] = []

        def load_artifacts_meta(self) -> tuple[_FakeBlob, int]:
            self.load_count += 1
            return _FakeBlob(generation=456), 456

        def update_meta_items(
            self,
            ipsw_metas,
            *,
            base_snapshot: IpswMetaSnapshot | None = None,
        ) -> IpswArtifactDb:
            self.update_calls.append((list(ipsw_metas), base_snapshot))
            if base_snapshot is None:
                return IpswArtifactDb()
            return base_snapshot.db

    monkeypatch.setattr("symx.ipsw.runners.AppleDbIpswImport", FakeImporter)
    storage = FakeStorage()

    import_meta_from_appledb(cast(IpswGcsStorage, storage))

    importer = FakeImporter.instances[0]
    assert importer.run_called is True
    assert storage.load_count == 1
    assert len(storage.update_calls) == 1
    artifacts, base_snapshot = storage.update_calls[0]
    assert artifacts == importer.new_artifacts
    assert base_snapshot is not None
    assert base_snapshot.db is importer.meta_db
    assert base_snapshot.generation == 456
