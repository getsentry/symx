import logging
import os

import sentry_sdk
import typer
from sentry_sdk.integrations.logging import SentryLogsHandler

from ._common import github_run_id
from ._ipsw.app import ipsw_app
from ._ota.app import ota_app
from ._sim.app import sim_app

SENTRY_DSN = os.environ.get("SENTRY_DSN", None)

app = typer.Typer()
app.add_typer(ota_app, name="ota")
app.add_typer(ipsw_app, name="ipsw")
app.add_typer(sim_app, name="sim")


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    setup_logs(verbose)
    setup_sentry()


def setup_sentry():
    if not SENTRY_DSN:
        return

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=1.0,
        enable_logs=True,
    )
    sentry_sdk.set_tag("github.run.id", github_run_id())


class AppendExtrasFormatter(logging.Formatter):
    RESERVED = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "taskName",
        "thread",
        "threadName",
        "processName",
        "process",
        "asctime",
        "message",
    }

    def format(self, record: logging.LogRecord):
        # Extract non-standard attributes
        extras = {k: v for k, v in record.__dict__.items() if k not in self.RESERVED}

        if extras:
            record.extra_str = f" {{{' '.join(f'{k}={v!r}' for k, v in extras.items())}}}"
        else:
            record.extra_str = ""

        return super().format(record)


def setup_logs(verbose: bool):
    lvl = logging.INFO
    fmt = "[%(levelname)s] %(asctime)s | %(name)s - - %(message)s%(extra_str)s"
    extra_std_out = logging.StreamHandler()
    extra_std_out.setFormatter(AppendExtrasFormatter(fmt=fmt))
    if verbose:
        lvl = logging.DEBUG
    logging.basicConfig(level=lvl, format=fmt, handlers=[SentryLogsHandler(level=logging.INFO), extra_std_out])
