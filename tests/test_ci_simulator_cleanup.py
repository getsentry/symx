"""Safety checks only; real CoreSimulator cleanup needs a disposable-runner check."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/remove-ci-simulators.sh"
WORKFLOWS = ROOT / ".github/workflows"


def run_cleanup(tmp_path: Path, *, on_ci: bool = True, fail_cleanup: bool = False) -> subprocess.CompletedProcess[str]:
    tools = tmp_path / "bin"
    tools.mkdir()
    # Isolate PATH completely: these stubs never invoke the host's simctl.
    # They test our safety boundaries, not runtime inventory or space reclamation.
    for tool in ("df", "xcrun"):
        stub = tools / tool
        stub.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS_PATH"\n'
            'if [[ "$FAIL_CLEANUP" == 1 && "$*" == "simctl runtime delete all" ]]; then\n'
            '  echo "simctl cleanup failed" >&2\n  exit 42\nfi\n'
        )
        stub.chmod(0o755)
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        env={
            "PATH": str(tools),
            "GITHUB_ACTIONS": "true" if on_ci else "",
            "RUNNER_OS": "macOS",
            "GITHUB_WORKSPACE": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "HOME": str(tmp_path),
            "CALLS_PATH": str(tmp_path / "calls.log"),
            "FAIL_CLEANUP": "1" if fail_cleanup else "0",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_cleanup_refuses_local_execution(tmp_path: Path) -> None:
    result = run_cleanup(tmp_path, on_ci=False)

    assert result.returncode != 0
    assert "only on a disposable macOS Actions extraction runner" in result.stderr
    assert not (tmp_path / "calls.log").exists()


def test_cleanup_is_opt_in_and_excludes_simulator_collection() -> None:
    reusable = (WORKFLOWS / "symx-runner-macos.yml").read_text()
    declaration = reusable.split("      remove_simulators:\n", 1)[1].split("    secrets:", 1)[0]
    assert "default: false" in declaration
    assert "if: ${{ inputs.remove_simulators }}" in reusable
    opted_in = {path.name for path in WORKFLOWS.glob("*.yml") if "remove_simulators: true" in path.read_text()}
    assert opted_in == {"symx-ipsw-extract.yml", "symx-ota-extract.yml"}
    simulator = (WORKFLOWS / "symx-simulator-extract.yml").read_text()
    assert "remove-ci-simulators" not in simulator
    assert "symx-runner-macos.yml" not in simulator


def test_cleanup_failure_blocks_extraction(tmp_path: Path) -> None:
    result = run_cleanup(tmp_path, fail_cleanup=True)

    assert result.returncode == 42
    assert "simctl cleanup failed" in result.stderr
    assert "after simulator cleanup (exit 42)" in result.stdout

    # continue-on-error allows notification, not extraction after a failed cleanup.
    workflow = (WORKFLOWS / "symx-runner-macos.yml").read_text()
    steps = workflow.split("      - name: ")
    cleanup_step = next(step for step in steps if "id: remove_simulators\n" in step)
    assert "set -o pipefail" in cleanup_step
    auth_step = next(step for step in steps if "id: auth\n" in step)
    assert "!inputs.remove_simulators || steps.remove_simulators.outcome == 'success'" in auth_step
    for step_id in ("setup_gcloud", "install_dependencies", "install_ipsw", "host_verification", "symx"):
        step = next(step for step in steps if f"id: {step_id}\n" in step)
        assert "steps.auth.outcome == 'success'" in step
    final_step = next(step for step in steps if step.startswith("Fail workflow after notification\n"))
    assert "steps.remove_simulators.outcome == 'failure'" in final_step
