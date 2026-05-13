# Iteration 002: GCS bucket scope and storage invariants

Date: 2026-05-12

## What changed

The working thesis now records that the simulation bucket is personal non-production storage:

```text
gs://apple_symbols/
```

Experiments may mutate this bucket when we agree on a concrete test. The bucket should still not be made inconsistent casually, because recreating mirrored/downloaded state costs time and makes simulation results noisy.

The thesis also now states the storage-safety rationale for metadata v2 more explicitly: v2 is not only a nicer data model, it maps better to GCS as the backing store.

## Commands run

No application commands were run.

Documentation was edited in the meta-v2 worktree:

```text
docs/meta-v2/000-working-thesis.md
docs/meta-v2/002-gcs-bucket-and-storage-invariants.md
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

No code or storage behavior was validated in this iteration.

## Mismatches or surprises

The previous safety language was too conservative for `gs://apple_symbols/` because it treated the bucket like unknown shared mutable state. The corrected framing is:

- it is non-prod and available for real experiments,
- but experiments should be purposeful and preferably namespaced,
- existing state is still worth preserving unless the test explicitly concerns rebuilding or mutating it.

## Production storage invariants clarified

Production Symx storage code should not overwrite or remove data casually.

Important invariants:

- Mirrored artifacts are durable replay inputs and should be treated as immutable once verified; changing mirror storage layout/lifecycle is out of scope for this investigation.
- Symbol output is the symbolicator-facing interface; existing symbol-store data must not be migrated or rekeyed. If future uploads ever use artifact IDs as bundle IDs, metadata must record the actual bundle ID used because the store will contain both legacy and new bundle-id forms.
- Metadata updates are the critical mutable path and must use conditional writes / optimistic concurrency.
- v1 metadata writes rewrite a whole domain metadata blob, so each writer must preserve many unrelated invariants.
- v2 artifact writes should usually touch one artifact object plus optional event/projection objects, reducing contention and blast radius.
- Adding new metadata in v2 can often mean adding detail/projection objects instead of changing every reader of a large JSON document.

## Changes to the production migration plan

This strengthens the case for shadowing v2 behind the existing storage facade early. The simulation should measure whether the v2 object layout actually reduces:

- bytes read/written per metadata mutation,
- unrelated data touched per mutation,
- generation conflicts / retry pressure,
- code complexity in update paths,
- risk of leaving a broad metadata document inconsistent.

It also changes the practical stance on the test bucket: once a concrete command exists, we can run real mutating simulations there under an explicit prefix instead of limiting ourselves to local-only reports.

## Next proposed iteration

Implement the first code slice:

- v2 artifact/detail models,
- deterministic artifact UID helpers,
- IPSW/OTA converters,
- v2 GCS prefix store skeleton,
- parity and metrics reporting for legacy metadata vs v2 materialization.
