from __future__ import annotations

from collections.abc import Callable

from pydantic import TypeAdapter, ValidationError

from symx.ota.model import OtaArtifact

ErrorFactory = Callable[[str], Exception]
_OTA_META = TypeAdapter(dict[str, OtaArtifact])


def parse_ota_meta_json(raw_json: str, error_factory: ErrorFactory) -> dict[str, OtaArtifact]:
    try:
        return _OTA_META.validate_json(raw_json)
    except ValidationError as error:
        raise error_factory("Unexpected OTA meta-data payload") from error
