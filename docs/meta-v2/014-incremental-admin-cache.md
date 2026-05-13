# Iteration 014: incremental admin cache instead of remote materialization

Date: 2026-05-12

## What changed

Clarified the preferred admin/read model.

The v2 store should not require a remote materialized metadata snapshot by default. A consistent point-in-time snapshot is not important for normal admin inspection. The old JSON files provided that property accidentally, but current admin/debug workflows do not need exact snapshot-time consistency.

Instead, admin can maintain a local SQLite cache incrementally:

1. list remote artifact/detail object metadata,
2. compare object generations against the local cache,
3. download only new or changed objects,
4. update local SQLite rows in a transaction,
5. keep enough generation metadata to validate or explain staleness,
6. revalidate expected state/generation before applying mutations.

The first sync still pays the full cost. Subsequent syncs should only download changed artifact/detail objects.

## Why this fits v2

Object-per-artifact metadata makes mutation cheaper and safer, but aggregate reads require a different strategy.

The right split is:

- canonical GCS object-per-artifact/detail records for mutable state,
- consumer-owned local projections/caches for aggregate reads,
- no mandatory store-level `views/current` or `snapshot.db` materialization.

Listing all objects may still take some time, but admin usage is occasional. More importantly, listing object metadata is much cheaper than downloading and parsing every artifact/detail object on every sync.

## Incremental admin cache sketch

Local SQLite tables could include:

```sql
remote_objects (
  object_name TEXT PRIMARY KEY,
  generation INTEGER NOT NULL,
  size_bytes INTEGER,
  updated_at TEXT,
  object_kind TEXT NOT NULL -- artifact | detail
)

artifacts (... normalized artifact columns ...)
ipsw_details (...)
ota_details (...)
sim_details (...)
```

Sync algorithm:

1. list `artifacts/` and `details/` prefixes,
2. compare `(object_name, generation)` with `remote_objects`,
3. download changed artifact objects,
4. download changed detail objects,
5. upsert changed rows and generation metadata in one SQLite transaction,
6. mark locally cached objects missing remotely as stale rather than deleting immediately,
7. expose sync statistics: listed, downloaded, unchanged, stale, failed.

No exact global consistency guarantee is required. If an artifact changes while sync is running, the next sync catches it. Admin apply must still validate current remote state before mutating.

## Commands run

No application commands were run for this documentation-only iteration.

## GCS access

None.

## Prefixes written

None.

## Validation results

Not applicable; documentation-only clarification.

## Mismatches or surprises

This corrects the previous temptation to make remote snapshot views a default part of the v2 model. They are useful experiments and may be useful for specific admin/export workflows, but they should not be required by the canonical metadata design.

## Changes to the production migration plan

Do not introduce a permanent remote materialization requirement unless a measured consumer needs it.

For admin migration, implement incremental local cache sync against v2 artifact/detail objects and keep generation-aware apply validation.

## Next proposed iteration

Add an inspection/validation command for the existing bootstrap prefix, or start a small prototype of the incremental admin-cache sync logic against the experiment prefix.
