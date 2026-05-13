# Iteration 010: dry-run bootstrap against apple_symbols

Date: 2026-05-12

## What changed

Ran the new bootstrap command in dry-run mode against the personal non-production bucket:

```text
gs://apple_symbols/
```

This validated that the command can read the real legacy metadata and convert it to normalized artifacts without writing any GCS objects.

## Commands run

```sh
uv run symx artifacts v2 bootstrap \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/dry-run-2026-05-12 \
  --dry-run
```

Output:

```json
{
  "artifact_count": 60704,
  "detail_count": 60704,
  "dry_run": true,
  "manifest_path": "experiments/meta-v2/dry-run-2026-05-12/manifests/bootstrap.json",
  "parity_mismatch_count": 0,
  "parity_ok": true,
  "parity_report_path": "experiments/meta-v2/dry-run-2026-05-12/reports/parity.json",
  "prefix": "experiments/meta-v2/dry-run-2026-05-12",
  "sample_written_objects": [
    "121410 objects would be written"
  ],
  "written_object_count": 0
}
```

## GCS access

Read-only.

Read from:

```text
gs://apple_symbols/ipsw_meta.json
gs://apple_symbols/ota_image_meta.json
```

No GCS objects were written.

## Prefixes written

None.

## Validation results

The real metadata converted cleanly:

- normalized artifacts: `60704`,
- detail records: `60704`,
- parity mismatches: `0`,
- worklist parity: OK.

Dry-run object count for a full experiment-prefix materialization:

```text
121410 objects
```

That count is:

- artifact records,
- detail records,
- parity report,
- bootstrap manifest.

## Mismatches or surprises

No parity mismatches.

The main observation is object count. Writing one artifact object plus one detail object per current artifact produces about 121k objects for the current real metadata snapshot. That is acceptable for a one-time/bootstrap experiment if we want it, but it is large enough that we should be deliberate before running the non-dry-run command.

This reinforces that v2 should not require every read consumer to list/read all artifact objects. Read-heavy consumers should use materialized views/worklists/snapshots. Object-per-artifact is primarily for lowering mutation contention/blast radius on update paths.

## Changes to the production migration plan

The real metadata supports the normalized artifact conversion and current worklist parity with no mismatches.

Before writing the full experiment prefix, decide whether the first materialized run should write:

1. full object-per-artifact + detail objects, or
2. a smaller first GCS experiment output such as manifest/report plus sharded JSONL bundles, then object-per-artifact in a later run.

Given the goal of testing GCS object-level semantics, full object-per-artifact is still the right experiment, but the 121k-object size should be acknowledged.

## Next proposed iteration

If approved, run the non-dry-run bootstrap with a unique prefix, for example:

```sh
uv run symx artifacts v2 bootstrap \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/bootstrap-2026-05-12-001
```

Then inspect manifest/report objects and measure wall-clock behavior for the write path.
