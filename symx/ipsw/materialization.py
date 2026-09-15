"""Typed outcomes for one IPSW DSC materialization attempt."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from symx.model import Arch


@dataclass(frozen=True)
class IpswDscMaterialized:
    """The requested DSC architecture was materialized."""

    arch: Arch | None
    extract_dir: Path


@dataclass(frozen=True)
class IpswDscNotPresent:
    """The requested architecture is not present in this IPSW."""

    arch: Arch
    message: str


class IpswDscUnavailableReason(StrEnum):
    """Why an IPSW DSC materialization attempt could not produce input."""

    INVOCATION_FAILED = "invocation_failed"
    NO_EXTRACT_DIR = "no_extract_dir"


@dataclass(frozen=True)
class IpswDscUnavailable:
    """Materialization failed for a reason other than architecture absence."""

    arch: Arch | None
    reason: IpswDscUnavailableReason
    message: str


IpswDscMaterializationAttempt = IpswDscMaterialized | IpswDscNotPresent | IpswDscUnavailable
