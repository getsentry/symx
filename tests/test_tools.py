import logging
from subprocess import CompletedProcess

import pytest

from symx import tools


def test_validate_shell_deps_exits_when_ipsw_is_too_old(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(tools, "ipsw_version", lambda: "3.1.710")

    def unexpected_subprocess(*args: object, **kwargs: object) -> CompletedProcess[bytes]:
        raise AssertionError("symsorter should not be checked when ipsw is too old")

    monkeypatch.setattr(tools.subprocess, "run", unexpected_subprocess)

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as error:
        tools.validate_shell_deps()

    assert error.value.code == 1
    assert "ipsw 3.1.710 is too old; version 3.1.711 or newer is required" in caplog.text


def test_validate_shell_deps_exits_when_ipsw_version_is_unparseable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(tools, "ipsw_version", lambda: "development")

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as error:
        tools.validate_shell_deps()

    assert error.value.code == 1
    assert "Unexpected ipsw version format: 'development'" in caplog.text


@pytest.mark.parametrize("version", ["3.1.711", "3.1.712", "3.2.0", "4.0.0"])
def test_validate_shell_deps_accepts_minimum_or_newer(version: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "ipsw_version", lambda: version)
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args=["./symsorter", "--version"], returncode=0, stdout=b"symsorter 1.0\n"
        ),
    )
    sentry_tags: list[tuple[str, str]] = []
    monkeypatch.setattr(tools.sentry_sdk, "set_tag", lambda key, value: sentry_tags.append((key, value)))

    tools.validate_shell_deps()

    assert ("ipsw.version", version) in sentry_tags
