from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

from symx.ota.model import OtaArtifact

ErrorFactory = Callable[[str], Exception]


def parse_ota_meta_json(raw_json: str, error_factory: ErrorFactory) -> dict[str, OtaArtifact]:
    raw_payload: object = json.loads(raw_json)
    if not isinstance(raw_payload, dict):
        raise error_factory("Unexpected OTA meta-data payload")

    ota_payload = cast(dict[object, object], raw_payload)
    result: dict[str, OtaArtifact] = {}
    for key, value in ota_payload.items():
        if not isinstance(key, str):
            raise error_factory("Unexpected OTA meta-data key type")
        if not isinstance(value, dict):
            raise error_factory("Unexpected OTA meta-data value type")
        result[key] = OtaArtifact(**cast(dict[str, Any], value))
    return result
