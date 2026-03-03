import datetime
import tempfile
from pathlib import Path

import sentry_sdk
import typer

from symx._common import parse_gcs_url, validate_shell_deps
from symx._ipsw.common import IpswPlatform
from symx._ipsw.extract import IpswExtractor
from symx._ipsw.runners import (
    import_meta_from_appledb,
    mirror as mirror_runner,
    extract as extract_runner,
    migrate as migrate_runner,
)
from symx._ipsw.storage.gcs import IpswGcsStorage

ipsw_app = typer.Typer()


def init_storage(local_dir: Path, storage: str) -> IpswGcsStorage | None:
    uri = parse_gcs_url(storage)
    if uri is None or uri.hostname is None:
        return None
    return IpswGcsStorage(local_dir, project=uri.username, bucket=uri.hostname)


@ipsw_app.command()
def meta_sync(storage: str = typer.Option(..., "--storage", "-s", help="Storage")) -> None:
    """
    Synchronize meta-data with appledb.
    """
    with sentry_sdk.start_transaction(op="ipsw.meta_sync", name="IPSW meta-sync"):
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
    Mirror all indexed artifacts.
    """
    with tempfile.TemporaryDirectory() as processing_dir:
        storage_backend = init_storage(Path(processing_dir), storage)
        if storage_backend:
            mirror_runner(storage_backend, datetime.timedelta(minutes=timeout))


@ipsw_app.command()
def extract(
    storage: str = typer.Option(..., "--storage", "-s", help="Storage"),
    timeout: int = typer.Option(
        345,
        "--timeout",
        "-t",
        help="timeout in minutes triggering an ordered shutdown after it elapsed",
    ),
) -> None:
    """
    Extract all mirrored artifacts and upload their binaries to the symbol store.
    """
    with tempfile.TemporaryDirectory() as processing_dir:
        storage_backend = init_storage(Path(processing_dir), storage)
        if storage_backend:
            extract_runner(storage_backend, datetime.timedelta(minutes=timeout))


@ipsw_app.command()
def extract_file(
    ipsw_file: Path = typer.Argument(..., help="Path to a local IPSW file", exists=True),
    platform: IpswPlatform = typer.Option(..., "--platform", "-p", help="Platform (e.g. iOS, macOS, watchOS)"),
    output_dir: Path | None = typer.Option(None, "--output", "-o", help="Output directory (default: temp dir)"),
) -> None:
    """
    Extract symbols from a local IPSW file.
    """
    with sentry_sdk.start_transaction(op="ipsw.extract_file", name=f"IPSW extract-file {ipsw_file.name}"):
        validate_shell_deps()

        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix="symx_ipsw_"))
            typer.echo(f"Output directory: {output_dir}")

        output_dir.mkdir(parents=True, exist_ok=True)

        extractor = IpswExtractor(platform, ipsw_file.name, output_dir, ipsw_file)
        symbols_dir = extractor.run()

        typer.echo(f"Extracted symbols to: {symbols_dir}")


@ipsw_app.command()
def migrate(
    storage: str = typer.Option(..., "--storage", "-s", help="Storage"),
) -> None:
    """
    Migrate/Maintain storage
    """
    with sentry_sdk.start_transaction(op="ipsw.migrate", name="IPSW migrate"):
        with tempfile.TemporaryDirectory() as processing_dir:
            storage_backend = init_storage(Path(processing_dir), storage)
            if storage_backend:
                migrate_runner(storage_backend)
