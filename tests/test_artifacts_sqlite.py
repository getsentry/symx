from __future__ import annotations

import gzip
import sqlite3
from datetime import date

from pydantic import HttpUrl

from symx.artifacts.convert import convert_ipsw_db, convert_ota_meta
from symx.artifacts.sqlite_store import build_sqlite_metadata_files, simulate_sqlite_state_update
from symx.ipsw.model import IpswArtifact, IpswArtifactDb, IpswPlatform, IpswReleaseStatus, IpswSource
from symx.ota.model import OtaArtifact
from tests.test_artifacts_storage import FakeBucket


def test_simulate_sqlite_state_update_round_trips_compressed_db(tmp_path, monkeypatch) -> None:
    bundles = [*convert_ipsw_db(_ipsw_db()), *convert_ota_meta(_ota_meta())]
    result = build_sqlite_metadata_files(
        storage="test-storage",
        output_dir=tmp_path,
        bundles=bundles,
        report=None,
    )
    compressed_payload = open(result.dbs[0].compressed_path, "rb").read()
    fake_bucket = FakeBucket({"experiments/sqlite/metadata.sqlite.gz": compressed_payload})

    class FakeClient:
        def __init__(self, project=None) -> None:
            pass

        def bucket(self, bucket_name: str):
            return fake_bucket

    monkeypatch.setattr("symx.artifacts.sqlite_store.Client", FakeClient)

    update_result = simulate_sqlite_state_update(
        "gs://apple_symbols/",
        "experiments/sqlite/metadata.sqlite.gz",
    )

    assert update_result.previous_state == "indexed"
    assert update_result.new_state == "ignored"
    assert update_result.integrity_check == "ok"
    assert update_result.generation_after > update_result.generation_before


def test_build_sqlite_metadata_files_writes_combined_and_domain_dbs(tmp_path) -> None:
    bundles = [*convert_ipsw_db(_ipsw_db()), *convert_ota_meta(_ota_meta())]

    result = build_sqlite_metadata_files(
        storage="test-storage",
        output_dir=tmp_path,
        bundles=bundles,
        report=None,
    )

    assert [db.name for db in result.dbs] == ["metadata", "ipsw", "ota"]
    combined = result.dbs[0]
    assert combined.artifact_count == 2
    assert combined.snapshot_counts.artifacts == 2
    assert combined.integrity_check == "ok"
    assert combined.raw_size_bytes > 0
    assert combined.compressed_size_bytes > 0

    with gzip.open(combined.compressed_path, "rb") as f:
        assert f.read(16).startswith(b"SQLite format 3")

    conn = sqlite3.connect(combined.path)
    try:
        rows = conn.execute("SELECT kind, COUNT(*) FROM artifacts GROUP BY kind ORDER BY kind").fetchall()
    finally:
        conn.close()
    assert rows == [("ipsw", 1), ("ota", 1)]


def _ipsw_db() -> IpswArtifactDb:
    artifact = IpswArtifact(
        platform=IpswPlatform.IOS,
        version="18.2",
        build="22C152",
        released=date.today(),
        release_status=IpswReleaseStatus.RELEASE,
        sources=[IpswSource(devices=["iPhone14,7"], link=HttpUrl("https://example.com/test.ipsw"))],
    )
    return IpswArtifactDb(artifacts={artifact.key: artifact})


def _ota_meta() -> dict[str, OtaArtifact]:
    return {
        "ota-key": OtaArtifact(
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
    }
