from typing import Optional

import typer
from rich import print

from urllib.parse import urlparse
import sentry_sdk
import os
import logging

from ._gcs import GoogleStorage
from ._ota import Ota
from ._maintenance import migrate

SENTRY_DSN = os.environ.get("SENTRY_DSN", None)

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=1.0,
    )

app = typer.Typer()

ota_app = typer.Typer()
app.add_typer(ota_app, name="ota")


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    lvl = logging.INFO
    fmt = "[%(levelname)s] %(asctime)s | %(name)s - - %(message)s"
    if verbose:
        lvl = logging.DEBUG
    logging.basicConfig(level=lvl, format=fmt)


def _init_storage(storage: str) -> Optional[GoogleStorage]:
    uri = urlparse(storage)
    if uri.scheme != "gs":
        print(
            '[bold red]Unsupported "--storage" URI-scheme used:[/bold red] currently symx supports "gs://" only'
        )
        return None

    if not uri.hostname:
        print(
            "[bold red]You must supply at least a bucket-name for the GCS storage[/bold red]"
        )
        return None

    return GoogleStorage(project=uri.username, bucket=uri.hostname)


@ota_app.command()
def mirror(storage: str = typer.Option(..., "--storage", "-s", help="Storage")) -> None:
    """
    Mirror OTA images to storage
    :param storage: URI to a supported storage backend
    """
    storage_backend = _init_storage(storage)
    if storage_backend:
        ota = Ota(storage=storage_backend)
        ota.mirror()


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
    storage_backend = _init_storage(storage)
    if storage_backend:
        migrate(storage_backend.bucket)
