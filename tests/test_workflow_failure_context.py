from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path


def load_module():
    spec = importlib.util.spec_from_file_location(
        "resolve_workflow_failure_context",
        Path("scripts/resolve_workflow_failure_context.py"),
    )
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


def parse_github_output(path: Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" not in line:
            name, value = line.split("=", 1)
            outputs[name] = value
            index += 1
            continue

        name, marker = line.split("<<", 1)
        index += 1
        value_lines: list[str] = []
        while lines[index] != marker:
            value_lines.append(lines[index])
            index += 1
        outputs[name] = "\n".join(value_lines)
        index += 1

    return outputs


class FakeResponse(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return None


def test_main_emits_no_failure_outputs(tmp_path):
    output_path = tmp_path / "github_output.txt"
    env = {
        "GITHUB_OUTPUT": str(output_path),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "getsentry/symx",
        "GITHUB_RUN_ID": "123",
        "FAILURE_CONTEXT_STEPS_JSON": '[{"name": "Checkout sources", "outcome": "success", "fallback_snippet": "uses actions/checkout@v4"}]',
    }

    assert MODULE.main(env) == 0

    assert parse_github_output(output_path) == {
        "failed": "false",
        "failed_step_name": "",
        "failed_step_snippet": "",
        "failed_step_url": "",
        "slack_payload_json": "",
    }


def test_main_emits_failure_details_from_log(tmp_path):
    log_path = tmp_path / "symx.log"
    log_path.write_text("first\n\x1b[31mboom\x1b[0m\n", encoding="utf-8")

    output_path = tmp_path / "github_output.txt"
    env = {
        "GITHUB_OUTPUT": str(output_path),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "getsentry/symx",
        "GITHUB_RUN_ID": "123",
        "FAILURE_CONTEXT_WORKFLOW_NAME": "Mirror IPSW artifacts",
        "FAILURE_CONTEXT_JOB_NAME": "IPSW mirror",
        "FAILURE_CONTEXT_LOGS_ROOT": str(tmp_path),
        "FAILURE_CONTEXT_STEPS_JSON": json.dumps(
            [
                {
                    "name": "Checkout sources",
                    "outcome": "success",
                    "fallback_snippet": "uses actions/checkout@v4",
                },
                {
                    "name": "IPSW mirror",
                    "outcome": "failure",
                    "log_path": "symx.log",
                    "fallback_snippet": "ipsw mirror",
                },
            ]
        ),
    }

    assert MODULE.main(env) == 0

    outputs = parse_github_output(output_path)
    assert outputs["failed"] == "true"
    assert outputs["failed_step_name"] == "IPSW mirror"
    assert outputs["failed_step_snippet"] == "first\nboom"
    assert outputs["failed_step_url"] == "https://github.com/getsentry/symx/actions/runs/123"
    assert json.loads(outputs["slack_payload_json"]) == {
        "text": (
            ":x: Workflow *Mirror IPSW artifacts* failed in job *IPSW mirror* at step *IPSW mirror*.\n"
            "```\nfirst\nboom\n```\n"
            "<https://github.com/getsentry/symx/actions/runs/123|Open failed step>"
        )
    }


def test_write_output_uses_non_colliding_marker(tmp_path, monkeypatch):
    class FakeUuid:
        def __init__(self, hex_value):
            self.hex = hex_value

    generated = iter([FakeUuid("collision"), FakeUuid("safe")])
    monkeypatch.setattr(MODULE.uuid, "uuid4", lambda: next(generated))

    output_path = tmp_path / "github_output.txt"
    MODULE.write_output(output_path, "failed_step_snippet", "before\nEOF_collision\nafter")

    assert output_path.read_text(encoding="utf-8") == (
        "failed_step_snippet<<EOF_safe\nbefore\nEOF_collision\nafter\nEOF_safe\n"
    )


def test_resolve_step_url_uses_step_anchor():
    env = {
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_REPOSITORY": "getsentry/symx",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
    }

    payload = """
    {
      "jobs": [
        {
          "name": "Extract IPSW symbols",
          "status": "in_progress",
          "html_url": "https://github.com/getsentry/symx/actions/runs/123/job/456",
          "steps": [
            {"name": "Checkout sources", "number": 1},
            {"name": "IPSW Extract", "number": 5}
          ]
        }
      ]
    }
    """

    def fake_urlopen(request, timeout):
        assert request.full_url.endswith("/actions/runs/123/attempts/2/jobs?per_page=100")
        assert request.headers["Authorization"] == "Bearer token"
        assert timeout == 10
        return FakeResponse(payload)

    assert (
        MODULE.resolve_step_url(env, "Extract IPSW symbols", "IPSW Extract", "token", fake_urlopen)
        == "https://github.com/getsentry/symx/actions/runs/123/job/456#step:5:1"
    )
