import datetime

import typer

from symx._ota.storage.gcs import init_storage
from symx._ota import OtaMirror, OtaExtract
from symx._ota.storage.maintenance import migrate

ota_app = typer.Typer()


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
