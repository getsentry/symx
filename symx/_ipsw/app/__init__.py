import datetime
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import typer

from symx._ipsw.runners import import_meta_from_appledb, mirror as mirror_runner
from symx._ipsw.storage.gcs import IpswGcsStorage

ipsw_app = typer.Typer()


def init_storage(local_dir: Path, storage: str) -> IpswGcsStorage | None:
    uri = urlparse(storage)
    if uri.scheme != "gs":
        print(
            '[bold red]Unsupported "--storage" URI-scheme used:[/bold red] currently'
            ' symx supports "gs://" only'
        )
        return None

    if not uri.hostname:
        print(
            "[bold red]You must supply at least a bucket-name for the GCS storage[/bold"
            " red]"
        )
        return None

    return IpswGcsStorage(local_dir, project=uri.username, bucket=uri.hostname)


@ipsw_app.command()
def meta_sync(
    storage: str = typer.Option(..., "--storage", "-s", help="Storage")
) -> None:
    """
    Synchronize meta-data with appledb.
    :return:
    """
    with tempfile.TemporaryDirectory() as processing_dir:
        storage_backend = init_storage(Path(processing_dir), storage)
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
    with tempfile.TemporaryDirectory() as processing_dir:
        storage_backend = init_storage(Path(processing_dir), storage)
        if storage_backend:
            mirror_runner(storage_backend, datetime.timedelta(minutes=timeout))
