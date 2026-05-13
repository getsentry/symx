# Iteration 016: local SQLite snapshot from v2 objects

Date: 2026-05-12

## What changed

Added an actual v2 snapshot command that reads the object-per-artifact/detail metadata under an experiment prefix and writes a local SQLite snapshot.

New command:

```sh
uv run symx artifacts v2 snapshot-from-v2 \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001 \
  --output /tmp/symx-v2-from-v2-snapshot.db \
  --max-workers 32
```

This is intentionally non-incremental: it lists v2 artifact objects, downloads each artifact and its referenced detail object, then builds a local SQLite DB.

## Files changed

```text
symx/artifacts/storage.py
symx/artifacts/app/__init__.py
symx/artifacts/snapshot.py
tests/test_artifacts_storage.py
docs/meta-v2/016-local-snapshot-from-v2.md
```

## Commands run

Help check:

```sh
uv run symx artifacts v2 snapshot-from-v2 --help
```

Actual v2 snapshot:

```sh
rm -f /tmp/symx-v2-from-v2-snapshot.db
/usr/bin/time -p uv run symx artifacts v2 snapshot-from-v2 \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001 \
  --output /tmp/symx-v2-from-v2-snapshot.db \
  --max-workers 32
```

Output:

```json
{
  "artifact_count": 60704,
  "output_path": "/tmp/symx-v2-from-v2-snapshot.db",
  "prefix": "experiments/meta-v2/bootstrap-2026-05-12-001",
  "snapshot_counts": {
    "artifacts": 60704,
    "ipsw_details": 15569,
    "ota_details": 45135,
    "sim_details": 0
  }
}
```

Timing:

```text
real 378.38
user 70.77
sys 14.80
```

Basic inspection:

```sh
ls -lh /tmp/symx-v2-from-v2-snapshot.db
sqlite3 /tmp/symx-v2-from-v2-snapshot.db \
  "select kind, count(*) from artifacts group by kind; select count(*) from ipsw_details; select count(*) from ota_details;"
```

Output:

```text
-rw-r--r-- 85M /tmp/symx-v2-from-v2-snapshot.db
ipsw|15569
ota|45135
15569
45135
```

## GCS access

Read-only.

Read from:

```text
gs://apple_symbols/experiments/meta-v2/bootstrap-2026-05-12-001/artifacts/...
gs://apple_symbols/experiments/meta-v2/bootstrap-2026-05-12-001/details/...
```

No GCS objects were written.

## Prefixes written

None.

Local file written:

```text
/tmp/symx-v2-from-v2-snapshot.db
```

## Validation results

The v2 object store can reconstruct a local SQLite snapshot with the expected counts:

```text
artifacts:    60704
ipsw_details: 15569
ota_details:  45135
sim_details:  0
```

## Mismatches or surprises

No semantic mismatches.

The non-incremental read cost is significant but not catastrophic: about 6m18s from this local environment with 32 workers. This is a useful upper-bound-ish data point for first admin sync or full validation. It reinforces the incremental admin cache plan: first sync is expensive, subsequent syncs should compare GCS generations and download only changed objects.

## Changes to the production migration plan

Actual v2 object reads are viable but should not be the default repeated admin path.

The preferred admin plan remains:

- first sync can perform a full v2 object read,
- subsequent syncs list object metadata and fetch only generation changes,
- admin mutations validate expected remote state/generation before applying changes.

## Next proposed iteration

Prototype the incremental local admin-cache sync metadata table or add a validation command that compares the local v2 snapshot against legacy counts/parity without re-downloading everything.
