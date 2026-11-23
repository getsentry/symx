import json
from unittest.mock import Mock

import sentry_sdk
from sentry_sdk.traces import NoOpSpan, Span

from symx.gcs import suppress_auto_instrumented_spans
from symx.telemetry import set_diagnostic


def test_bulk_upload_suppression_is_scoped() -> None:
    original_scope = sentry_sdk.get_current_scope()
    original_span = original_scope.span

    with suppress_auto_instrumented_spans():
        assert sentry_sdk.get_current_scope() is not original_scope
        assert isinstance(sentry_sdk.get_current_scope().span, NoOpSpan)
        assert original_scope.span is original_span

    assert sentry_sdk.get_current_scope() is original_scope
    assert original_scope.span is original_span


def test_diagnostic_record_preserves_fields_on_span() -> None:
    span = Mock(spec=Span)
    directories = [{"path": "output", "exists": False}]

    set_diagnostic(span, "extract", {"stderr": "timed out", "directories": directories})

    span.set_attribute.assert_any_call("extract.stderr", "timed out")
    span.set_attribute.assert_any_call("extract.directories", json.dumps(directories))
