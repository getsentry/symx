import typer
from rich import print

from urllib.parse import urlparse
import sentry_sdk
import os
import logging

from ._gcs import GoogleStorage
from ._ota import Ota

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
def main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    lvl = logging.INFO
    fmt = "[%(levelname)s] %(asctime)s | %(name)s - - %(message)s"
    if verbose:
        lvl = logging.DEBUG
    logging.basicConfig(level=lvl, format=fmt)


@ota_app.command()
def mirror(storage: str = typer.Option(..., "--storage", "-s", help="Storage")) -> None:
    """
    Mirror OTA images to storage
    """
    uri = urlparse(storage)
    if uri.scheme == "gs":
        storage_backend = GoogleStorage(project=uri.username, bucket=uri.hostname)
        ota = Ota(storage=storage_backend)
        ota.download()
    else:
        print(
            '[bold red]Unsupported "--storage" URI-scheme used:[/bold red] currently symx supports "gs://" only'
        )
