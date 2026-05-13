# Iteration 009: GCS experiment-prefix bootstrap command

Date: 2026-05-12

## What changed

Added a GCS-backed experiment bootstrap path for normalized metadata.

New command:

```sh
uv run symx artifacts v2 bootstrap \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/<run-id>
```

Dry-run mode:

```sh
uv run symx artifacts v2 bootstrap \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/<run-id> \
  --dry-run
```

The command:

1. reads legacy root metadata objects from the configured bucket,
2. converts IPSW/OTA legacy metadata to normalized artifact/detail records,
3. builds the same parity report as the local report command,
4. writes artifact/detail records, parity report, and manifest under the configured prefix,
5. uses create-only GCS writes for experiment objects,
6. writes the manifest last.

Safety behavior:

- by default the prefix must start with `experiments/`,
- `--allow-non-experiment-prefix` is required to write elsewhere,
- root legacy metadata, mirrors, and symbols are not modified,
- existing experiment objects are not overwritten.

## Files changed

```text
symx/artifacts/storage.py
symx/artifacts/app/__init__.py
tests/test_artifacts_storage.py
docs/meta-v2/009-gcs-bootstrap-command.md
```

## Commands run

```sh
uv run pytest tests/test_artifacts_v2.py tests/test_artifacts_storage.py
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run symx artifacts v2 bootstrap --help >/tmp/symx-bootstrap-help.txt
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest
```

Final full-suite result:

```text
175 passed, 3 skipped
```

## GCS access

None.

No reads or writes were made against:

```text
gs://apple_symbols/
```

## Prefixes written

None.

## Validation results

New storage tests verify:

- empty prefixes are rejected,
- bootstrap writes normalized objects under the experiment prefix,
- bootstrap leaves legacy root metadata objects untouched,
- bootstrap writes the expected object count for a small fixture,
- bootstrap refuses to overwrite an existing experiment object.

The new bootstrap CLI help renders successfully.

## Mismatches or surprises

The Google Cloud Storage typings in this environment do not expose all keyword arguments used by the runtime SDK for some Blob methods. The bootstrap storage code therefore avoids those keywords where necessary and relies on create-only `if_generation_match=0` for writes.

## Changes to the production migration plan

We now have a concrete command for the first real bucket simulation.

The first run should probably be a dry run:

```sh
uv run symx artifacts v2 bootstrap \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/<run-id> \
  --dry-run
```

If the dry run can read and validate the legacy metadata, run the same command without `--dry-run` using a unique prefix.

## Next proposed iteration

Run the dry-run bootstrap against `gs://apple_symbols/`, inspect the parity summary, then decide whether to write the experiment prefix.
