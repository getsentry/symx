"""Strict models for the versioned external ``ipsw`` OTA DSC report.

These Pydantic models validate an untrusted subprocess boundary. Trusted runtime
request/result values for the adapter live in :mod:`symx.ota.model.materialization`.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OtaDscReportFile(BaseModel):
    """One file entry from the ipsw schema-1 OTA DSC report."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    path: str = Field(min_length=1)
    arch: str
    source: str = Field(min_length=1)


class OtaDscReportError(BaseModel):
    """One structured failure from the ipsw schema-1 OTA DSC report."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    phase: str = Field(min_length=1)
    source: str
    message: str = Field(min_length=1)


class OtaDscReport(BaseModel):
    """Strict typed boundary for ``ipsw ota extract --dyld --json``."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    complete: bool
    files: list[OtaDscReportFile]
    errors: list[OtaDscReportError]
