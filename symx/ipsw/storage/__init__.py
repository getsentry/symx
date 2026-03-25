from pathlib import Path
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Protocol

from symx.ipsw.model import IpswArtifact, IpswArtifactDb, IpswSource


class IpswStorage(Protocol):
    local_dir: Path

    def update_meta_item(self, ipsw_meta: IpswArtifact) -> IpswArtifactDb: ...

    def artifact_iter(
        self, filter_fun: Callable[[Iterable[IpswArtifact]], Sequence[IpswArtifact]]
    ) -> Iterator[IpswArtifact]: ...

    def upload_ipsw(self, artifact: IpswArtifact, downloaded_source: tuple[Path, IpswSource]) -> IpswArtifact: ...

    def download_ipsw(self, ipsw_source: IpswSource) -> Path | None: ...

    def upload_symbols(
        self,
        prefix: str,
        bundle_id: str,
        artifact: IpswArtifact,
        source_idx: int,
        binary_dir: Path,
    ) -> None: ...

    def clean_local_dir(self) -> None: ...
