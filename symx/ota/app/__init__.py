import datetime
import tempfile
from pathlib import Path

import sentry_sdk
import typer

from symx.common import validate_shell_deps
from symx.ota.storage.gcs import init_storage
from symx.ota.extract import extract_symbols
from symx.ota.runners import OtaExtract, OtaMirror
from symx.ota.storage.maintenance import migrate

ota_app = typer.Typer()


@ota_app.command()
def mirror(
    storage: str = typer.Option(..., "--storage", "-s", help="URI to a supported storage backend"),
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
        from symx.common import Timeout

        ota = OtaMirror(storage=storage_backend)
        ota.mirror(Timeout(datetime.timedelta(minutes=timeout)))


@ota_app.command()
def extract(
    storage: str = typer.Option(..., "--storage", "-s", help="URI to a supported storage backend"),
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
        from symx.common import Timeout

        ota = OtaExtract(storage=storage_backend)
        ota.extract(Timeout(datetime.timedelta(minutes=timeout)))


@ota_app.command()
def extract_file(
    ota_file: Path = typer.Argument(..., help="Path to a local OTA zip file", exists=True),
    platform: str = typer.Option(..., "--platform", "-p", help="Platform (e.g. ios, macos, watchos)"),
    version: str = typer.Option(..., "--version", "-V", help="OS version (e.g. 18.2)"),
    build: str = typer.Option(..., "--build", "-b", help="Build identifier (e.g. 22C152)"),
    output_dir: Path | None = typer.Option(
        None, "--output", "-o", help="Output directory for extracted symbols (default: temp dir)"
    ),
    bundle_id: str | None = typer.Option(None, "--bundle-id", help="Bundle ID for symsorter (default: auto-generated)"),
) -> None:
    """
    Extract symbols from a local OTA file.
    """
    with sentry_sdk.start_transaction(op="ota.extract_file", name=f"OTA extract-file {ota_file.name}"):
        validate_shell_deps()

        if bundle_id is None:
            bundle_id = f"ota_{platform}_{version}_{build}"

        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix="symx_ota_"))
            typer.echo(f"Output directory: {output_dir}")

        symbol_dirs = extract_symbols(
            local_ota=ota_file,
            platform=platform,
            version=version,
            build=build,
            bundle_id=bundle_id,
            work_dir=output_dir,
        )

        if symbol_dirs:
            typer.echo("Extracted symbols to:")
            for d in symbol_dirs:
                typer.echo(f"  {d}")
        else:
            typer.echo("No symbols extracted.", err=True)
            raise typer.Exit(code=1)


@ota_app.command()
def migrate_storage(storage: str = typer.Option(..., "--storage", "-s", help="Storage")) -> None:
    """
    Migrate the data on the store to the latest layout.
    This currently does not include any versioning or migration history, but could later become a goal. Right now it is
    just the entry point for a GHA.
    :param storage: URI to a supported storage backend
    """
    with sentry_sdk.start_transaction(op="ota.migrate", name="OTA migrate"):
        storage_backend = init_storage(storage)
        if storage_backend:
            migrate(storage_backend)
