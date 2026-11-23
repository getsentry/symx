import logging
import os

import sentry_sdk
import typer
from sentry_sdk.integrations.logging import LoggingIntegration

from .model import github_run_id
from .admin.app import admin_app
from .gha.app import gha_app
from .ipsw.app import ipsw_app
from .ota.app import ota_app
from .sim.app import sim_app
from .stats.app import stats_app

SENTRY_DSN = os.environ.get("SENTRY_DSN", None)

app = typer.Typer()
app.add_typer(admin_app, name="admin")
app.add_typer(gha_app, name="gha")
app.add_typer(ota_app, name="ota")
app.add_typer(ipsw_app, name="ipsw")
app.add_typer(sim_app, name="sim")
app.add_typer(stats_app, name="stats")


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
        integrations=[LoggingIntegration(level=logging.INFO, breadcrumb_level=logging.INFO, event_level=logging.ERROR)],
        data_collection={
            "user_info": False,
            "http_bodies": [],
            "graphql": {"document": False, "variables": False},
            "gen_ai": {"inputs": False, "outputs": False},
            "mcp": {"inputs": False, "outputs": False},
            "database_query_data": False,
            "queues": False,
            "stack_frame_variables": {"mode": "denylist"},
            "http_headers": {
                "request": {
                    "mode": "denylist",
                    "terms": ["forwarded", "-ip", "remote-", "via", "-user"],
                },
            },
            "cookies": {
                "mode": "denylist",
                "terms": ["forwarded", "-ip", "remote-", "via", "-user"],
            },
            "url_query_params": {
                "mode": "denylist",
                "terms": ["forwarded", "-ip", "remote-", "via", "-user"],
            },
        },
    )

    run_id = github_run_id()
    sentry_sdk.set_tag("github.run.id", run_id)
    sentry_sdk.set_attribute("github.run.id", run_id)


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
    logging.basicConfig(level=lvl, format=fmt, handlers=[extra_std_out])
