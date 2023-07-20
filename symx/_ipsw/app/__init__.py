import datetime

import typer

from symx._gcs import init_storage
from symx._ipsw.runners import import_meta_from_appledb, mirror as mirror_runner

ipsw_app = typer.Typer()


@ipsw_app.command()
def meta_sync(
    storage: str = typer.Option(..., "--storage", "-s", help="Storage")
) -> None:
    """
    Synchronize meta-data with appledb.
    :return:
    """
    storage_backend = init_storage(storage)
    if storage_backend:
        import_meta_from_appledb(storage_backend)


@ipsw_app.command()
def mirror(
    storage: str = typer.Option(..., "--storage", "-s", help="Storage"),
    timeout: int = typer.Option(
        345,
        "--timeout",
        "-t",
        help="timeout in minutes triggering an ordered shutdown after it elapsed",
    ),
) -> None:
    """
    Synchronize meta-data with appledb.
    :return:
    """
    storage_backend = init_storage(storage)
    if storage_backend:
        mirror_runner(storage_backend, datetime.timedelta(minutes=timeout))
