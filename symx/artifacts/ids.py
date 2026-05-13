"""Deterministic identifiers for normalized artifact records."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from symx.artifacts.model import ArtifactKind

_SEPARATOR = "\0"


def stable_hash(parts: Iterable[str]) -> str:
    """Return a deterministic SHA-256 hex digest for ordered identity parts."""

    digest = hashlib.sha256()
    first = True
    for part in parts:
        if not first:
            digest.update(_SEPARATOR.encode())
        digest.update(part.encode("utf-8"))
        first = False
    return digest.hexdigest()


def ipsw_artifact_uid(artifact_key: str, source_link: str) -> str:
    return f"{ArtifactKind.IPSW.value}:{stable_hash((ArtifactKind.IPSW.value, artifact_key, source_link))}"


def ota_artifact_uid(ota_key: str) -> str:
    return f"{ArtifactKind.OTA.value}:{stable_hash((ArtifactKind.OTA.value, ota_key))}"


def sim_artifact_uid(runtime_identity: str, arch: str) -> str:
    return f"{ArtifactKind.SIM.value}:{stable_hash((ArtifactKind.SIM.value, runtime_identity, arch))}"


def artifact_object_path(artifact_uid: str) -> str:
    return f"artifacts/{artifact_uid}.json"


def detail_object_path(kind: ArtifactKind, artifact_uid: str) -> str:
    return f"details/{kind.value}/{artifact_uid}.json"
