#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SNIPPET_LINE_LIMIT = 20
SNIPPET_CHAR_LIMIT = 1500
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    outcome: str | None
    fallback_snippet: str
    log_path: str | None = None


class FailureContextConfigError(ValueError):
    pass


def output_marker(value: str) -> str:
    value_lines = set(value.splitlines())
    while True:
        marker = f"EOF_{uuid.uuid4().hex}"
        if marker not in value_lines:
            return marker


def write_output(output_path: Path, name: str, value: str) -> None:
    with output_path.open("a", encoding="utf-8") as handle:
        if "\n" in value:
            marker = output_marker(value)
            handle.write(f"{name}<<{marker}\n{value}\n{marker}\n")
        else:
            handle.write(f"{name}={value}\n")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def resolve_log_path(log_path: str | None, logs_root: str | None) -> Path | None:
    if not log_path:
        return None

    path = Path(log_path)
    if path.is_absolute() or not logs_root:
        return path

    return Path(logs_root) / path


def load_snippet(log_path: str | None, fallback: str, logs_root: str | None = None) -> str:
    path = resolve_log_path(log_path, logs_root)
    if path and path.exists():
        lines = [strip_ansi(line.rstrip()) for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
        lines = [line for line in lines if line.strip()]
        if lines:
            snippet = "\n".join(lines[-SNIPPET_LINE_LIMIT:])
            if len(snippet) > SNIPPET_CHAR_LIMIT:
                snippet = snippet[-SNIPPET_CHAR_LIMIT:]
            return snippet

    return fallback or "See step logs in GitHub Actions."


def parse_steps(raw_steps: str) -> list[WorkflowStep]:
    try:
        data = json.loads(raw_steps)
    except json.JSONDecodeError as error:
        raise FailureContextConfigError("FAILURE_CONTEXT_STEPS_JSON is not valid JSON") from error

    if not isinstance(data, list):
        raise FailureContextConfigError("FAILURE_CONTEXT_STEPS_JSON must be a JSON array")

    steps: list[WorkflowStep] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise FailureContextConfigError(f"Step {index} is not a JSON object")

        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise FailureContextConfigError(f"Step {index} is missing a valid 'name'")

        outcome = item.get("outcome")
        if outcome is not None and not isinstance(outcome, str):
            raise FailureContextConfigError(f"Step {index} has an invalid 'outcome'")

        fallback_snippet = item.get("fallback_snippet", "")
        if not isinstance(fallback_snippet, str):
            raise FailureContextConfigError(f"Step {index} has an invalid 'fallback_snippet'")

        log_path = item.get("log_path")
        if log_path is not None and not isinstance(log_path, str):
            raise FailureContextConfigError(f"Step {index} has an invalid 'log_path'")

        steps.append(
            WorkflowStep(
                name=name,
                outcome=outcome,
                fallback_snippet=fallback_snippet,
                log_path=log_path,
            )
        )

    return steps


def find_failed_step(steps: list[WorkflowStep]) -> WorkflowStep | None:
    return next((step for step in steps if step.outcome == "failure"), None)


def default_step_url(env: Mapping[str, str]) -> str:
    return f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"


def select_job(jobs: list[dict[str, Any]], job_name: str) -> dict[str, Any] | None:
    matches = [job for job in jobs if job.get("name") == job_name]
    if not matches:
        matches = [job for job in jobs if job_name and job_name in str(job.get("name", ""))]
    if len(matches) > 1:
        in_progress = [job for job in matches if job.get("status") == "in_progress"]
        if len(in_progress) == 1:
            matches = in_progress
    return matches[0] if matches else None


def fetch_jobs(
    env: Mapping[str, str],
    token: str,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> list[dict[str, Any]]:
    api_url = (
        f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}"
        f"/actions/runs/{env['GITHUB_RUN_ID']}/attempts/{env['GITHUB_RUN_ATTEMPT']}/jobs?per_page=100"
    )
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    jobs = payload.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def resolve_step_url(
    env: Mapping[str, str],
    job_name: str,
    failed_step_name: str,
    token: str,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    step_url = default_step_url(env)
    if not token or not job_name:
        return step_url

    try:
        jobs = fetch_jobs(env, token, urlopen)
        job = select_job(jobs, job_name)
        if not job:
            return step_url

        step_url = str(job.get("html_url") or step_url)
        for workflow_step in job.get("steps", []):
            if workflow_step.get("name") != failed_step_name:
                continue
            step_number = workflow_step.get("number")
            if step_number is not None and job.get("html_url"):
                return f"{job['html_url']}#step:{step_number}:1"
            return step_url
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        print(f"Could not resolve failed step URL: {error}", file=sys.stderr)

    return step_url


def build_slack_text(
    workflow_name: str,
    job_name: str,
    failed_step_name: str,
    failed_step_snippet: str,
    failed_step_url: str,
) -> str:
    return (
        f":x: Workflow *{workflow_name}* failed in job *{job_name}* at step *{failed_step_name}*.\n"
        f"```\n{failed_step_snippet}\n```\n"
        f"<{failed_step_url}|Open failed step>"
    )


def build_slack_payload_json(text: str) -> str:
    return json.dumps({"text": text})


def emit_failure_outputs(
    output_path: Path,
    failed_step: WorkflowStep | None,
    *,
    logs_root: str | None = None,
    workflow_name: str = "",
    job_name: str = "",
    step_url: str = "",
) -> None:
    if failed_step is None:
        write_output(output_path, "failed", "false")
        write_output(output_path, "failed_step_name", "")
        write_output(output_path, "failed_step_snippet", "")
        write_output(output_path, "failed_step_url", "")
        write_output(output_path, "slack_payload_json", "")
        return

    failed_step_snippet = load_snippet(failed_step.log_path, failed_step.fallback_snippet, logs_root)
    write_output(output_path, "failed", "true")
    write_output(output_path, "failed_step_name", failed_step.name)
    write_output(output_path, "failed_step_snippet", failed_step_snippet)
    write_output(output_path, "failed_step_url", step_url)
    write_output(
        output_path,
        "slack_payload_json",
        build_slack_payload_json(
            build_slack_text(workflow_name, job_name, failed_step.name, failed_step_snippet, step_url)
        ),
    )


def main(env: Mapping[str, str] | None = None) -> int:
    current_env = os.environ if env is None else env
    output_path = Path(current_env["GITHUB_OUTPUT"])

    try:
        steps = parse_steps(current_env.get("FAILURE_CONTEXT_STEPS_JSON", "[]"))
    except FailureContextConfigError as error:
        print(str(error), file=sys.stderr)
        return 2

    failed_step = find_failed_step(steps)
    if failed_step is None:
        emit_failure_outputs(output_path, None)
        return 0

    job_name = current_env.get("FAILURE_CONTEXT_JOB_NAME", "")
    step_url = resolve_step_url(
        current_env,
        job_name=job_name,
        failed_step_name=failed_step.name,
        token=current_env.get("GITHUB_TOKEN", ""),
    )
    emit_failure_outputs(
        output_path,
        failed_step,
        logs_root=current_env.get("FAILURE_CONTEXT_LOGS_ROOT"),
        workflow_name=current_env.get("FAILURE_CONTEXT_WORKFLOW_NAME", current_env.get("GITHUB_WORKFLOW", "")),
        job_name=job_name,
        step_url=step_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
