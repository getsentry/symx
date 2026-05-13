"""GCS-backed storage helpers for normalized artifact metadata experiments."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from google.api_core.exceptions import PreconditionFailed
from google.cloud.storage import Blob, Bucket, Client
from pydantic import BaseModel, Field

from symx.admin.meta_json import parse_ota_meta_json
from symx.artifacts.convert import convert_ipsw_db, convert_ota_meta
from symx.artifacts.model import ArtifactBundle
from symx.artifacts.report import ArtifactParityReport, ArtifactReportError, build_parity_report
from symx.gcs import parse_gcs_url
from symx.ipsw.model import ARTIFACTS_META_JSON as IPSW_ARTIFACTS_META_JSON
from symx.ipsw.model import IpswArtifactDb
from symx.ota.model import ARTIFACTS_META_JSON as OTA_ARTIFACTS_META_JSON
from symx.ota.model import OtaArtifact

BOOTSTRAP_MANIFEST_SCHEMA_VERSION = 1


class ArtifactStorageError(RuntimeError):
    pass


class ExistingObjectError(ArtifactStorageError):
    def __init__(self, object_name: str) -> None:
        self.object_name = object_name
        super().__init__(f"Refusing to overwrite existing GCS object: {object_name}")


class LegacyMetaObject(BaseModel):
    name: str
    generation: int
    size_bytes: int | None = None


class LegacyMetaSnapshot(BaseModel):
    ipsw_db: IpswArtifactDb
    ipsw_meta_object: LegacyMetaObject
    ota_meta: dict[str, OtaArtifact]
    ota_meta_object: LegacyMetaObject


class BootstrapManifest(BaseModel):
    schema_version: int = BOOTSTRAP_MANIFEST_SCHEMA_VERSION
    generated_at: str
    storage: str
    prefix: str
    ipsw_meta_object: LegacyMetaObject
    ota_meta_object: LegacyMetaObject
    artifact_count: int
    detail_count: int
    totals_by_kind: dict[str, int]
    state_counts_by_kind: dict[str, dict[str, int]]
    parity_ok: bool
    parity_mismatch_count: int
    artifact_prefix: str
    detail_prefix: str
    parity_report_path: str
    manifest_path: str


class BootstrapResult(BaseModel):
    manifest: BootstrapManifest
    parity_report: ArtifactParityReport
    dry_run: bool
    written_object_count: int = 0
    sample_written_objects: list[str] = Field(default_factory=list)


class ArtifactGcsPrefixStore:
    def __init__(self, bucket: Bucket, prefix: str, storage_uri: str) -> None:
        self.bucket = bucket
        self.prefix = normalize_prefix(prefix)
        self.storage_uri = storage_uri

    @classmethod
    def from_storage_uri(cls, storage: str, prefix: str) -> "ArtifactGcsPrefixStore":
        uri = parse_gcs_url(storage)
        if uri is None or uri.hostname is None:
            raise ArtifactStorageError(f"Unsupported storage URI: {storage}")

        client: Client = Client(project=uri.username)
        return cls(client.bucket(uri.hostname), prefix, storage)

    def object_name(self, relative_path: str) -> str:
        normalized_relative = normalize_relative_path(relative_path)
        return str(PurePosixPath(self.prefix) / normalized_relative)

    def load_legacy_meta(self) -> LegacyMetaSnapshot:
        ipsw_text, ipsw_object = self._download_text_with_generation(IPSW_ARTIFACTS_META_JSON)
        ota_text, ota_object = self._download_text_with_generation(OTA_ARTIFACTS_META_JSON)
        return LegacyMetaSnapshot(
            ipsw_db=IpswArtifactDb.model_validate_json(ipsw_text),
            ipsw_meta_object=ipsw_object,
            ota_meta=parse_ota_meta_json(ota_text, ArtifactReportError),
            ota_meta_object=ota_object,
        )

    def build_bootstrap(self) -> tuple[list[ArtifactBundle], ArtifactParityReport, BootstrapManifest]:
        snapshot = self.load_legacy_meta()
        bundles = [*convert_ipsw_db(snapshot.ipsw_db), *convert_ota_meta(snapshot.ota_meta)]
        report = build_parity_report(snapshot.ipsw_db, snapshot.ota_meta)
        manifest = self._build_manifest(snapshot, report, len(bundles))
        return bundles, report, manifest

    def write_bootstrap(
        self,
        bundles: list[ArtifactBundle],
        report: ArtifactParityReport,
        manifest: BootstrapManifest,
        max_workers: int = 16,
    ) -> BootstrapResult:
        self._ensure_bootstrap_markers_absent(manifest)
        objects = self._bootstrap_objects(bundles, report, manifest)
        written = write_json_objects_create_only(self.bucket, objects, max_workers=max_workers)
        return BootstrapResult(
            manifest=manifest,
            parity_report=report,
            dry_run=False,
            written_object_count=len(written),
            sample_written_objects=written[:20],
        )

    def dry_run_bootstrap(self) -> BootstrapResult:
        bundles, report, manifest = self.build_bootstrap()
        object_count = len(self._bootstrap_objects(bundles, report, manifest))
        return BootstrapResult(
            manifest=manifest,
            parity_report=report,
            dry_run=True,
            written_object_count=0,
            sample_written_objects=[f"{object_count} objects would be written"],
        )

    def bootstrap(self, dry_run: bool = False, max_workers: int = 16) -> BootstrapResult:
        bundles, report, manifest = self.build_bootstrap()
        if dry_run:
            object_count = len(self._bootstrap_objects(bundles, report, manifest))
            return BootstrapResult(
                manifest=manifest,
                parity_report=report,
                dry_run=True,
                written_object_count=0,
                sample_written_objects=[f"{object_count} objects would be written"],
            )
        return self.write_bootstrap(bundles, report, manifest, max_workers=max_workers)

    def _ensure_bootstrap_markers_absent(self, manifest: BootstrapManifest) -> None:
        for object_name in (manifest.manifest_path, manifest.parity_report_path):
            if self.bucket.blob(object_name).exists():
                raise ExistingObjectError(object_name)

    def _download_text_with_generation(self, object_name: str) -> tuple[str, LegacyMetaObject]:
        blob = self.bucket.blob(object_name)
        if not blob.exists():
            raise ArtifactStorageError(f"Missing required legacy metadata object: {object_name}")
        blob.reload()
        with NamedTemporaryFile() as temp_file:
            blob.download_to_filename(temp_file.name)
            text = Path(temp_file.name).read_text()
        blob_size_obj = getattr(blob, "size", None)
        blob_size = int(blob_size_obj) if isinstance(blob_size_obj, int) else None
        return text, LegacyMetaObject(
            name=object_name,
            generation=int(blob.generation or 0),
            size_bytes=blob_size,
        )

    def _build_manifest(
        self,
        snapshot: LegacyMetaSnapshot,
        report: ArtifactParityReport,
        artifact_count: int,
    ) -> BootstrapManifest:
        return BootstrapManifest(
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            storage=self.storage_uri,
            prefix=self.prefix,
            ipsw_meta_object=snapshot.ipsw_meta_object,
            ota_meta_object=snapshot.ota_meta_object,
            artifact_count=artifact_count,
            detail_count=artifact_count,
            totals_by_kind=report.totals_by_kind,
            state_counts_by_kind=report.state_counts_by_kind,
            parity_ok=report.ok,
            parity_mismatch_count=len(report.mismatches),
            artifact_prefix=self.object_name("artifacts"),
            detail_prefix=self.object_name("details"),
            parity_report_path=self.object_name("reports/parity.json"),
            manifest_path=self.object_name("manifests/bootstrap.json"),
        )

    def _bootstrap_objects(
        self,
        bundles: list[ArtifactBundle],
        report: ArtifactParityReport,
        manifest: BootstrapManifest,
    ) -> dict[str, str]:
        objects: dict[str, str] = {}
        for bundle in bundles:
            artifact_path = self.object_name(f"artifacts/{bundle.artifact.artifact_uid}.json")
            detail_path = self.object_name(bundle.artifact.detail_path)
            objects[artifact_path] = bundle.artifact.model_dump_json(indent=2) + "\n"
            objects[detail_path] = bundle.detail.model_dump_json(indent=2) + "\n"

        objects[manifest.parity_report_path] = report.model_dump_json(indent=2) + "\n"
        objects[manifest.manifest_path] = manifest.model_dump_json(indent=2) + "\n"
        return objects


def normalize_prefix(prefix: str) -> str:
    normalized = prefix.strip().strip("/")
    if not normalized:
        raise ArtifactStorageError("A non-empty metadata prefix is required")
    if "//" in normalized:
        raise ArtifactStorageError(f"Invalid metadata prefix: {prefix}")
    return normalized


def normalize_relative_path(path: str) -> str:
    normalized = path.strip().strip("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        raise ArtifactStorageError(f"Invalid relative object path: {path}")
    return normalized


def write_json_objects_create_only(bucket: Bucket, objects: dict[str, str], max_workers: int = 16) -> list[str]:
    if len(objects) != len(set(objects)):
        raise ArtifactStorageError("Refusing to write duplicate object names")

    if max_workers <= 1:
        return [_write_json_object_create_only(bucket, name, payload) for name, payload in objects.items()]

    written: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_write_json_object_create_only, bucket, name, payload): name
            for name, payload in objects.items()
        }
        for future in as_completed(futures):
            written.append(future.result())
    return sorted(written)


def _write_json_object_create_only(bucket: Bucket, object_name: str, payload: str) -> str:
    blob: Blob = bucket.blob(object_name)
    try:
        blob.upload_from_string(
            payload,
            if_generation_match=0,
        )
    except PreconditionFailed as exc:
        raise ExistingObjectError(object_name) from exc
    return object_name
