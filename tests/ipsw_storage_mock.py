"""In-memory IpswStorage implementation for testing."""

import shutil
from pathlib import Path
from typing import Callable, Iterator, Iterable, Sequence

from symx.ipsw.common import IpswArtifact, IpswArtifactDb, IpswSource


class InMemoryIpswStorage:
    """
    A test double for IpswStorage that keeps everything in memory + a temp directory.

    - Artifacts are stored in a plain dict (the "database").
    - "Uploaded" IPSW files are copied into a `mirror/` subdirectory under local_dir.
    - "Uploaded" symbols are recorded but not stored (we just track that upload_symbols was called).
    - download_ipsw copies from the mirror dir back to local_dir.
    """

    def __init__(self, local_dir: Path) -> None:
        self.local_dir = local_dir
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._mirror_dir = self.local_dir / "mirror"
        self._mirror_dir.mkdir(exist_ok=True)

        self._db = IpswArtifactDb()

        # Track calls for test assertions
        self.uploaded_ipsws: list[tuple[str, str]] = []  # (artifact_key, source_file_name)
        self.uploaded_symbols: list[tuple[str, str]] = []  # (prefix, bundle_id)
        self.meta_updates: list[str] = []  # artifact keys
        self.clean_local_dir_count: int = 0

    def seed_artifact(self, artifact: IpswArtifact) -> None:
        """Add an artifact to the in-memory db for test setup."""
        self._db.upsert(artifact.key, artifact)

    def get_artifact(self, key: str) -> IpswArtifact | None:
        """Retrieve an artifact from the in-memory db for test assertions."""
        return self._db.get(key)

    def update_meta_item(self, ipsw_meta: IpswArtifact) -> IpswArtifactDb:
        self._db.upsert(ipsw_meta.key, ipsw_meta)
        self.meta_updates.append(ipsw_meta.key)
        return self._db

    def artifact_iter(
        self, filter_fun: Callable[[Iterable[IpswArtifact]], Sequence[IpswArtifact]]
    ) -> Iterator[IpswArtifact]:
        """
        Mimics the GCS version: re-reads the db each iteration, yields the first match,
        stops when filter returns empty.
        """
        while True:
            filtered = filter_fun(self._db.artifacts.values())
            if len(filtered) == 0:
                break
            yield filtered[0]

    def upload_ipsw(self, artifact: IpswArtifact, downloaded_source: tuple[Path, IpswSource]) -> IpswArtifact:
        ipsw_file, source = downloaded_source
        source_idx = artifact.sources.index(source)

        if not ipsw_file.is_file():
            raise RuntimeError("Path to upload must be a file")

        # "Upload" by copying to mirror dir
        mirror_path = f"mirror/ipsw/{artifact.platform}/{artifact.version}/{artifact.build}/{source.file_name}"
        dest = self.local_dir / mirror_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ipsw_file, dest)

        artifact.sources[source_idx].mirror_path = mirror_path
        from symx.common import ArtifactProcessingState

        artifact.sources[source_idx].processing_state = ArtifactProcessingState.MIRRORED
        artifact.sources[source_idx].update_last_run()

        self.uploaded_ipsws.append((artifact.key, source.file_name))
        return artifact

    def download_ipsw(self, ipsw_source: IpswSource) -> Path | None:
        if ipsw_source.mirror_path is None:
            return None

        mirror_file = self.local_dir / ipsw_source.mirror_path
        if not mirror_file.exists():
            return None

        local_path = self.local_dir / ipsw_source.file_name
        shutil.copy2(mirror_file, local_path)
        return local_path

    def upload_symbols(
        self,
        prefix: str,
        bundle_id: str,
        artifact: IpswArtifact,
        source_idx: int,
        binary_dir: Path,
    ) -> None:
        from symx.common import ArtifactProcessingState

        artifact.sources[source_idx].processing_state = ArtifactProcessingState.SYMBOLS_EXTRACTED
        artifact.sources[source_idx].update_last_run()
        self.update_meta_item(artifact)
        self.uploaded_symbols.append((prefix, bundle_id))

    def clean_local_dir(self) -> None:
        self.clean_local_dir_count += 1
        for item in self.local_dir.iterdir():
            if item == self._mirror_dir:
                continue
            if item.is_dir():
                shutil.rmtree(item)
            elif item.is_file() and item.suffix == ".ipsw":
                item.unlink()
