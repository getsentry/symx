import datetime
import logging
import os

import sentry_sdk
import typer

from ._common import github_run_id
from ._gcs import init_storage
from ._ipsw.app import ipsw_app
from ._maintenance import migrate
from ._ota import OtaMirror, OtaExtract

SENTRY_DSN = os.environ.get("SENTRY_DSN", None)

app = typer.Typer()
ota_app = typer.Typer()
# TODO: move all ota stuffs into its own submodule
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


@ota_app.command()
def mirror(
    storage: str = typer.Option(
        ..., "--storage", "-s", help="URI to a supported storage backend"
    ),
    timeout: int = typer.Option(
        345,
        "--timeout",
        "-t",
        help="timeout in minutes triggering an ordered shutdown after it elapsed",
    ),
) -> None:
    """
    Mirror OTA images to storage
    """
    storage_backend = init_storage(storage)
    if storage_backend:
        ota = OtaMirror(storage=storage_backend)
        ota.mirror(datetime.timedelta(minutes=timeout))


@ota_app.command()
def extract(
    storage: str = typer.Option(
        ..., "--storage", "-s", help="URI to a supported storage backend"
    ),
    timeout: int = typer.Option(
        345,
        "--timeout",
        "-t",
        help="timeout in minutes triggering an ordered shutdown after it elapsed",
    ),
) -> None:
    """
    Extract dyld_shared_cache and symbols from OTA images to storage
    """
    storage_backend = init_storage(storage)
    if storage_backend:
        ota = OtaExtract(storage=storage_backend)
        ota.extract(datetime.timedelta(minutes=timeout))


@ota_app.command()
def migrate_storage(
    storage: str = typer.Option(..., "--storage", "-s", help="Storage")
) -> None:
    """
    Migrate the data on the store to the latest layout.
    This currently does not include any versioning or migration history, but could later become a goal. Right now it is
    just the entry point for a GHA.
    :param storage: URI to a supported storage backend
    """
    storage_backend = init_storage(storage)
    if storage_backend:
        migrate(storage_backend)
