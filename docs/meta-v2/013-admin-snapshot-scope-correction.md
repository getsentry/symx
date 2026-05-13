# Iteration 013: admin snapshot scope correction

Date: 2026-05-12

## What changed

The previous snapshot-materialization conclusion was too broad.

A consistent global metadata snapshot is not a core requirement of metadata-v2. It came for free with the legacy monolithic JSON files, but most processing paths do not need it. If metadata changes while an admin/debug view is being built, that is acceptable as long as later mutations validate expected state/generation before applying changes.

The snapshot/view concept is therefore demoted:

- not canonical metadata,
- not required for normal processing,
- not automatically written by bootstrap,
- at most an optional admin/debug projection.

The bootstrap command was adjusted back to writing only:

```text
artifacts/...
details/...
reports/parity.json
manifests/bootstrap.json
```

The separate experimental snapshot command remains available for now:

```sh
uv run symx artifacts v2 snapshot \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001
```

But its role is exploratory/admin-specific, not part of the required v2 data model.

## Answer: where would a single snapshot object come from?

If we keep a single snapshot object, it should probably be created by the admin sync path, not by every metadata write and not by bootstrap by default.

Likely options:

1. **On-demand GitHub Actions admin-sync artifact**
   - The local admin CLI dispatches an admin-sync workflow, as it does today.
   - The workflow builds a SQLite snapshot from the current metadata source.
   - The snapshot is uploaded as a GitHub Actions artifact.
   - The local admin tool downloads that artifact.
   - No persistent GCS view is required.

2. **On-demand GCS admin/debug object**
   - A manually invoked command/workflow writes `views/snapshot.db` or `views/snapshot.db.gz` under an experiment/admin prefix.
   - The admin tool downloads it from GCS.
   - This is useful for experiments, but not required as a permanent production interface.

3. **No store-level materialized snapshot**
   - Admin sync lists v2 artifact/detail object metadata when needed.
   - Initial sync downloads all rows into a local SQLite cache.
   - Later syncs download only objects whose GCS generation changed.
   - This may still list many objects, but it avoids re-downloading all metadata and does not require a persistent remote materialization.
   - Admin use is occasional and does not require strict snapshot consistency.

The current experiment has proven option 2 is possible, but it has not proven that it is necessary. Option 3 is preferable unless admin usage proves listing cost alone is unacceptable.

## Files changed

```text
symx/artifacts/storage.py
symx/artifacts/app/__init__.py
tests/test_artifacts_storage.py
docs/meta-v2/009-gcs-bootstrap-command.md
docs/meta-v2/013-admin-snapshot-scope-correction.md
```

## Commands run

```sh
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest tests/test_artifacts_storage.py
```

## GCS access

None in this correction iteration.

## Prefixes written

None.

## Validation results

Storage tests now assert that bootstrap does **not** write `views/snapshot.db`, while the explicit snapshot command still can write one snapshot object.

## Mismatches or surprises

The earlier analysis incorrectly treated aggregate read materialization as a generally necessary model component. The more precise statement is:

- object-per-artifact is the mutable metadata layout,
- aggregate snapshots are optional admin/debug projections,
- processing correctness should not depend on a globally consistent metadata snapshot.

## Changes to the production migration plan

Do not add permanent `views/current.json` / `views/snapshot.db` requirements until an actual consumer needs them.

For admin, prefer the current pattern conceptually, but make it incremental:

- build a local/admin SQLite cache on first sync,
- store remote object names and GCS generations for artifact/detail objects,
- on later syncs, list remote object metadata and fetch only changed objects,
- validate mutations at apply time,
- tolerate that the read view may be stale or mildly inconsistent.

## Next proposed iteration

Continue with validation/inspection of the written bootstrap prefix, but keep snapshot materialization as optional.
