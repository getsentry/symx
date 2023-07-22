import logging
import os

import sentry_sdk
import typer

from ._common import github_run_id
from ._ipsw.app import ipsw_app
from ._ota.app import ota_app

SENTRY_DSN = os.environ.get("SENTRY_DSN", None)

app = typer.Typer()
app.add_typer(ota_app, name="ota")
app.add_typer(ipsw_app, name="ipsw")


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    lvl = logging.INFO
    fmt = "[%(levelname)s] %(asctime)s | %(name)s - - %(message)s"
    if verbose:
        lvl = logging.DEBUG
    logging.basicConfig(level=lvl, format=fmt)

    sentry_sdk.set_tag("github.run.id", github_run_id())

    if SENTRY_DSN:
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=1.0,
        )
