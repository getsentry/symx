from datetime import date, datetime

from pydantic import HttpUrl

from symx.admin.actions import (
    AdminActionKind,
    AdminStore,
    ApplyBatchRequest,
    ApplyBatchResult,
    ApplyBatchStatus,
    IpswTarget,
    WorkerDispatchResult,
    WorkerDispatchStatus,
)
from symx.admin.remote import append_apply_summary, execute_apply_request
from symx.ipsw.model import (
    IpswArtifact,
    IpswArtifactDb,
    IpswArtifactHashes,
    IpswPlatform,
    IpswReleaseStatus,
    IpswSource,
)
from symx.model import ArtifactProcessingState


class _FakeBlob:
    def __init__(self, payload: str, generation: int) -> None:
        self.payload = payload
        self.generation = generation
        self.uploads: list[tuple[str, int]] = []

    def reload(self) -> None:
        return None

    def download_as_text(self) -> str:
        return self.payload

    def upload_from_string(self, payload: str, if_generation_match: int) -> None:
        self.uploads.append((payload, if_generation_match))
        self.payload = payload
        self.generation += 1


class _FakeBucket:
    def __init__(self, blob: _FakeBlob) -> None:
        self._blob = blob

    def blob(self, name: str) -> _FakeBlob:
        return self._blob


def _make_ipsw_payload() -> str:
    ipsw_db = IpswArtifactDb(
        artifacts={
            "iOS_18.0_22A100": IpswArtifact(
                platform=IpswPlatform.IOS,
                version="18.0",
                build="22A100",
                released=date(2024, 9, 1),
                release_status=IpswReleaseStatus.RELEASE,
                sources=[
                    IpswSource(
                        devices=["iPhone17,1"],
                        link=HttpUrl("https://updates.cdn-apple.com/test.ipsw"),
                        hashes=IpswArtifactHashes(sha1="abc", sha2=None),
                        size=123,
                        processing_state=ArtifactProcessingState.SYMBOLS_EXTRACTED,
                        mirror_path="mirror/ipsw/iOS/18.0/22A100/test.ipsw",
                        last_run=111,
                        last_modified=datetime(2024, 9, 3, 12, 0, 0),
                    )
                ],
            )
        }
    )
    return ipsw_db.model_dump_json()


def test_execute_apply_request_refuses_stale_generation(monkeypatch) -> None:
    request = ApplyBatchRequest(
        store=AdminStore.IPSW,
        action=AdminActionKind.QUEUE_EXTRACT,
        snapshot_id="ipsw-101__ota-202",
        base_generation=100,
        reason="retry extract",
        targets=(IpswTarget(artifact_key="iOS_18.0_22A100", link="https://updates.cdn-apple.com/test.ipsw"),),
    )
    fake_blob = _FakeBlob(_make_ipsw_payload(), generation=101)

    monkeypatch.setattr("symx.admin.remote._bucket_for_storage", lambda storage: _FakeBucket(fake_blob))

    result = execute_apply_request("gs://bucket", request)

    assert result.status == ApplyBatchStatus.STALE_GENERATION
    assert result.remote_generation == 101
    assert fake_blob.uploads == []


def test_append_apply_summary_appends_to_existing_file(tmp_path) -> None:
    summary_path = tmp_path / "summary.md"
    summary_path.write_text("existing summary\n")
    result = ApplyBatchResult(
        status=ApplyBatchStatus.APPLIED,
        store=AdminStore.IPSW,
        action=AdminActionKind.QUEUE_EXTRACT,
        snapshot_id="ipsw-101__ota-202",
        base_generation=101,
        remote_generation=101,
        targets=(IpswTarget(artifact_key="iOS_18.0_22A100", link="https://updates.cdn-apple.com/test.ipsw"),),
        reason="retry extract",
        applied_count=1,
        message="applied",
    )

    append_apply_summary(summary_path, result)

    content = summary_path.read_text()
    assert content.startswith("existing summary\n")
    assert "# Admin apply: applied" in content
    assert content.count("# Admin apply: applied") == 1


def test_execute_apply_request_returns_worker_warning_after_successful_apply(monkeypatch) -> None:
    request = ApplyBatchRequest(
        store=AdminStore.IPSW,
        action=AdminActionKind.QUEUE_EXTRACT,
        snapshot_id="ipsw-101__ota-202",
        base_generation=101,
        reason="retry extract",
        targets=(IpswTarget(artifact_key="iOS_18.0_22A100", link="https://updates.cdn-apple.com/test.ipsw"),),
    )
    fake_blob = _FakeBlob(_make_ipsw_payload(), generation=101)

    monkeypatch.setattr("symx.admin.remote._bucket_for_storage", lambda storage: _FakeBucket(fake_blob))
    monkeypatch.setattr(
        "symx.admin.remote.ensure_worker_running",
        lambda request, status_callback=None: WorkerDispatchResult(
            workflow="symx-ipsw-extract.yml",
            status=WorkerDispatchStatus.DISPATCH_FAILED,
            detail="gh workflow run failed",
        ),
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "555")

    result = execute_apply_request("gs://bucket", request)

    assert result.status == ApplyBatchStatus.APPLIED_WITH_WORKER_WARNING
    assert result.applied_count == 1
    assert fake_blob.uploads[0][1] == 101
    uploaded_db = IpswArtifactDb.model_validate_json(fake_blob.payload)
    uploaded_source = uploaded_db.artifacts["iOS_18.0_22A100"].sources[0]
    assert uploaded_source.processing_state == ArtifactProcessingState.MIRRORED
    assert uploaded_source.last_run == 555
