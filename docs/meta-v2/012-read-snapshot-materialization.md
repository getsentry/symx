# Iteration 012: read snapshot materialization

Date: 2026-05-12

## What changed

The first full bootstrap made the read-path trade-off concrete: object-per-artifact is a better mutation/write model, but it is not acceptable as the routine aggregate read model.

A simple listing already shows the issue:

```sh
time gcloud storage ls gs://apple_symbols/experiments/meta-v2/bootstrap-2026-05-12-001/details/ota | wc -l
```

Observed by the user:

```text
45135
gcloud storage ls   2.35s user 0.10s system 25% cpu 9.487 total
wc -l  0.00s user 0.00s system 0% cpu 9.486 total
```

Listing one detail prefix took almost 10 seconds, and that did not download or parse any objects. Therefore consumers like admin sync must not reconstruct a consistent snapshot by listing/reading all artifact/detail objects.

Added a materialized SQLite snapshot view:

```text
views/snapshot.db
```

New command:

```sh
uv run symx artifacts v2 snapshot \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001
```

The command currently reads legacy root metadata, builds the normalized snapshot DB locally, and writes one create-only snapshot object under the prefix. Future v2-authoritative snapshot builders can use the same snapshot schema but source the rows from v2 artifact state.

Future bootstrap runs also write `views/snapshot.db` before publishing the final manifest.

## Files changed

```text
symx/artifacts/snapshot.py
symx/artifacts/storage.py
symx/artifacts/app/__init__.py
tests/test_artifacts_storage.py
docs/meta-v2/012-read-snapshot-materialization.md
```

## Commands run

Targeted verification while implementing:

```sh
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest tests/test_artifacts_storage.py
```

Dry-run snapshot:

```sh
uv run symx artifacts v2 snapshot \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001 \
  --dry-run
```

Output:

```json
{
  "dry_run": true,
  "prefix": "experiments/meta-v2/bootstrap-2026-05-12-001",
  "snapshot_counts": {
    "artifacts": 60704,
    "ipsw_details": 15569,
    "ota_details": 45135,
    "sim_details": 0
  },
  "snapshot_db_path": "experiments/meta-v2/bootstrap-2026-05-12-001/views/snapshot.db",
  "written_object_count": 0
}
```

Snapshot write:

```sh
/usr/bin/time -p uv run symx artifacts v2 snapshot \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001
```

Output:

```json
{
  "dry_run": false,
  "prefix": "experiments/meta-v2/bootstrap-2026-05-12-001",
  "snapshot_counts": {
    "artifacts": 60704,
    "ipsw_details": 15569,
    "ota_details": 45135,
    "sim_details": 0
  },
  "snapshot_db_path": "experiments/meta-v2/bootstrap-2026-05-12-001/views/snapshot.db",
  "written_object_count": 1
}
```

Timing:

```text
real 21.86
user 3.39
sys 0.64
```

Inspect snapshot object size:

```sh
gcloud storage ls -l gs://apple_symbols/experiments/meta-v2/bootstrap-2026-05-12-001/views/snapshot.db
```

Output:

```text
89214976 bytes (85.08MiB)
```

Download snapshot:

```sh
rm -f /tmp/symx-v2-snapshot.db
/usr/bin/time -p gcloud storage cp \
  gs://apple_symbols/experiments/meta-v2/bootstrap-2026-05-12-001/views/snapshot.db \
  /tmp/symx-v2-snapshot.db
```

Output:

```text
Average throughput: 27.0MiB/s
real 5.07
user 3.23
sys 1.27
```

Basic query:

```sh
sqlite3 /tmp/symx-v2-snapshot.db "select kind, count(*) from artifacts group by kind;"
```

Output:

```text
ipsw|15569
ota|45135
```

Compression experiment:

```sh
/usr/bin/time -p gzip -c /tmp/symx-v2-snapshot.db > /tmp/symx-v2-snapshot.db.gz
ls -lh /tmp/symx-v2-snapshot.db.gz
```

Output:

```text
real 0.76
-rw-r--r-- 18M /tmp/symx-v2-snapshot.db.gz
```

## GCS access

Read:

```text
gs://apple_symbols/ipsw_meta.json
gs://apple_symbols/ota_image_meta.json
```

Wrote:

```text
gs://apple_symbols/experiments/meta-v2/bootstrap-2026-05-12-001/views/snapshot.db
```

No root legacy metadata, mirror objects, or symbol objects were modified.

## Prefixes written

```text
experiments/meta-v2/bootstrap-2026-05-12-001/views/snapshot.db
```

## Validation results

The materialized snapshot contains:

```text
artifacts:    60704
ipsw_details: 15569
ota_details:  45135
sim_details:  0
```

The snapshot can be downloaded as one object and queried locally with SQLite.

## Mismatches or surprises

No semantic mismatches.

The uncompressed SQLite DB is 85MiB, larger than the two legacy JSON files together. But it downloads as one object in ~5 seconds locally, and gzip reduces it to 18MiB in under a second. This suggests the production/admin sync path should probably publish a compressed snapshot DB or compressed SQLite-compatible bundle.

## Changes to the production migration plan

The model should be described explicitly as:

- object-per-artifact/detail metadata for low-contention mutable state,
- materialized snapshots/views for aggregate readers,
- optional worklist views for runner selection paths.

Admin sync should consume a materialized snapshot object, not list/read artifact/detail objects. The `views/current.json` pointer from the earlier design remains important: it should point at a complete immutable snapshot object or snapshot directory after all view files are uploaded.

## Next proposed iteration

Decide whether to make compressed snapshots the default view artifact:

```text
views/snapshot.db.gz
```

If yes, update the snapshot writer to upload gzip-compressed SQLite and teach the future admin sync path to download/decompress it before opening SQLite.
