"""Helpers for preserving Symx diagnostics on streamed Sentry spans."""

import json

from sentry_sdk.traces import Span


def set_diagnostic(span: Span, key: str, value: object) -> None:
    """Keep record fields searchable; serialize nested records on the same span."""
    if isinstance(value, dict):
        for field, item in value.items():
            _set_value(span, f"{key}.{field}", item)
    else:
        _set_value(span, key, value)


def _set_value(span: Span, key: str, value: object) -> None:
    if isinstance(value, (str, bool, int, float)):
        span.set_attribute(key, value)
    elif value is not None:
        span.set_attribute(key, json.dumps(value))
