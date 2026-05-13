# Iteration 011: full bootstrap and connection-pool follow-up

Date: 2026-05-12

## What changed

Ran the first full non-dry-run metadata-v2 bootstrap against the personal non-production bucket.

Also updated the bootstrap store to configure the GCS client's underlying HTTP connection pool to match `--max-workers`. The run used `--max-workers 32`, which exceeded the default urllib3 pool size of 10 and produced many warnings like:

```text
Connection pool is full, discarding connection: storage.googleapis.com. Connection pool size: 10
```

The command still succeeded, but future thread-pool runs should not create avoidable connection-pool churn/noise.

## Commands run

Full bootstrap:

```sh
/usr/bin/time -p uv run symx artifacts v2 bootstrap \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001 \
  --max-workers 32
```

Verification after connection-pool adjustment:

```sh
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest tests/test_artifacts_storage.py
```

## GCS access

Read:

```text
gs://apple_symbols/ipsw_meta.json
gs://apple_symbols/ota_image_meta.json
```

Wrote under:

```text
gs://apple_symbols/experiments/meta-v2/bootstrap-2026-05-12-001/
```

No root legacy metadata, mirror objects, or symbol objects were modified.

## Prefixes written

```text
experiments/meta-v2/bootstrap-2026-05-12-001/artifacts/...
experiments/meta-v2/bootstrap-2026-05-12-001/details/ipsw/...
experiments/meta-v2/bootstrap-2026-05-12-001/details/ota/...
experiments/meta-v2/bootstrap-2026-05-12-001/reports/parity.json
experiments/meta-v2/bootstrap-2026-05-12-001/manifests/bootstrap.json
```

The implementation writes the manifest last.

## Validation results

Bootstrap output summary:

```json
{
  "artifact_count": 60704,
  "detail_count": 60704,
  "dry_run": false,
  "manifest_path": "experiments/meta-v2/bootstrap-2026-05-12-001/manifests/bootstrap.json",
  "parity_mismatch_count": 0,
  "parity_ok": true,
  "parity_report_path": "experiments/meta-v2/bootstrap-2026-05-12-001/reports/parity.json",
  "prefix": "experiments/meta-v2/bootstrap-2026-05-12-001",
  "written_object_count": 121410
}
```

Wall-clock timing:

```text
real 660.58
user 99.12
sys 23.44
```

Post-change targeted storage tests passed.

## Mismatches or surprises

No parity mismatches.

The main surprise was operational rather than semantic: writing ~121k tiny GCS objects with 32 worker threads hit the default urllib3 connection pool size and logged repeated pool-full warnings. This likely reduced connection reuse and made logs noisy.

Follow-up code change:

- `ArtifactGcsPrefixStore.from_storage_uri(..., connection_pool_size=...)` now configures the GCS client's HTTP adapter pool size.
- The CLI passes `--max-workers` as the connection pool size.

This keeps the thread-pool and HTTP connection pool aligned.

A later iteration added `views/snapshot.db` to future bootstrap outputs. The bootstrap result recorded in this document predates that addition; the existing experiment prefix was updated with a snapshot in iteration 012.

## Changes to the production migration plan

The first litmus test succeeded: the real metadata can be materialized into object-per-artifact/detail GCS layout at current scale.

Observed cost at this scale:

- about 121k object writes,
- about 11 minutes wall-clock from a local machine with 32 workers before connection-pool tuning,
- no semantic/parity mismatches.

This confirms that any larger migration path must explicitly treat high object count as normal, not exceptional. It also reinforces that read paths should use generated views/snapshots/worklists rather than listing every artifact object for routine queries.

## Next proposed iteration

Add validation/inspection commands for an already-written bootstrap prefix:

```sh
uv run symx artifacts v2 validate \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001
```

This should read manifest/report and spot-check artifact/detail objects without re-reading all 121k objects by default. A later `--full` mode can perform a complete scan if needed.
