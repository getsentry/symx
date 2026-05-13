# Iteration 015: cost accounting and the real v2 trade-off

Date: 2026-05-12

## What changed

Recorded the main insight from the first real bootstrap experiments: metadata-v2 is not an obviously superior replacement for the current JSON metadata model. It is a trade-off.

The current monolithic JSON model is awkward and makes individual metadata writes high-stakes, but it also provides a cheap full-domain aggregate snapshot by default. Metadata-v2 maps better to per-artifact GCS mutation semantics, but it makes aggregate reads and cache/projection strategies explicit costs.

Without accounting for those costs, the v2 model looks artificially cheap.

## What v2 improves

Potential wins:

- smaller mutation blast radius,
- lower write contention for independent artifact updates,
- better fit for GCS generation-matched object updates,
- easier source/model extensibility via metadata-source-specific detail records,
- easier per-artifact transition/event history,
- less need for every writer to preserve unrelated rows in a large shared JSON blob.

## What v2 costs

Observed or expected costs:

- many more GCS objects,
- slower aggregate discovery/listing,
- no free full-domain snapshot,
- local/admin cache logic becomes more important,
- more model surface area:
  - artifact identity vs symbol bundle identity,
  - metadata source vs processing kind,
  - domain/source detail schemas,
  - legacy backrefs,
  - optional projections/caches,
- more code paths to validate and keep deployable during migration,
- more decisions about consumer-specific read models.

Concrete experiment data:

- current real metadata converted to `60704` normalized artifacts,
- full object-per-artifact/detail bootstrap wrote `121410` objects,
- full bootstrap took about `660s` locally with `--max-workers 32` before connection-pool tuning,
- listing only the OTA detail prefix (`45135` objects) took about `9.5s`, before downloading/parsing any object contents,
- the optional SQLite snapshot was `85MiB` uncompressed and `18MiB` gzipped.

## Valid outcomes

A positive outcome of this evaluation does not have to be a v2 migration.

Another good outcome is a decision document that records:

- the measured trade-offs,
- the effects on code and operational complexity,
- why we should not attempt this migration now,
- what smaller changes would capture the most important benefits with lower cost.

That document would still be useful because it prevents future redesign attempts from treating the same costs as unknown or negligible.

## Corrected framing

The better framing is not:

> v2 is the obviously better metadata model.

It is:

> v2 may be worth it if reduced mutation contention/blast radius and source extensibility are valuable enough to pay for increased aggregate-read, cache, and model complexity.

The current JSON model should be treated as a carefully engineered baseline with useful accidental properties, not as a naive implementation to replace casually.

## Impact on migration plan

The migration should continue to be evidence-driven:

- compare mutation path complexity and safety,
- compare write conflict behavior,
- measure listing/sync/cache costs,
- avoid mandatory remote materializations unless a consumer proves it needs them,
- keep v1/v2 shadowing and rollback/replay practical,
- document every newly discovered cost before committing to cutover.

## Commands run

No application commands were run for this documentation-only iteration.

## GCS access

None.

## Prefixes written

None.

## Validation results

Not applicable; documentation-only cost/trade-off recording.

## Next proposed iteration

Continue with focused experiments that test the actual claimed v2 wins, especially mutation/update paths and generation-conflict behavior, instead of only expanding the model surface.
