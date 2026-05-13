# Iteration 001: shadow v2 from the first implementation

Date: 2026-05-12

## What changed

The migration thesis was corrected away from an offline-first/bootstrap-only interpretation.

The updated direction is:

- v2 should exist as a shadow store from the first useful implementation,
- v1 remains production-authoritative initially,
- current storage-facing seams should grow comparable v1/v2 read and write paths,
- each touched consumer may need temporary double modeling so outputs can be compared,
- admin/stats are not excluded as early consumers, but they must be explicit parity consumers if touched,
- the migration can still end in a full one-time metadata cutover if the shadow simulation proves the new model clearly enough.

## Commands run

No application or GCS commands were run.

Repository/worktree commands already performed before this iteration:

```sh
git worktree add -b symx-meta-v2 ../symx_meta_v2 main
```

Documentation files changed locally in the new worktree:

```text
docs/meta-v2/000-working-thesis.md
docs/meta-v2/001-shadow-from-iteration-one.md
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

The earlier plan incorrectly treated bootstrap/report/validate tooling as the main first phase and framed admin/stats as something to avoid as an early consumer.

The corrected view is that the hard part is not which consumer comes first. The hard part is that every deployable, cross-checkable migration path requires temporary dual read/write modeling for touched consumers.

## Changes to the production migration plan

The production migration plan now emphasizes a storage facade / comparison harness earlier:

1. Build v2 models and deterministic IDs.
2. Build v2 GCS storage under a configurable prefix.
3. Put v1 and v2 behind comparable storage-facing operations where practical.
4. Keep v1 authoritative but shadow-write/materialize v2 from the same domain mutation.
5. Compare work selection, state transitions, persisted post-write state, CAS behavior, metadata bytes, object counts, and duration.
6. Use those results to choose between:
   - one-time v2 production migration and full metadata behavior switch, or
   - staged cutover with explicit safe points.

Rollback remains plausible because v1 JSON snapshots can be retained and replayed if v2 is abandoned before it becomes the only authoritative write model.

## Next proposed iteration

Implement the first code slice without production behavior changes:

- v2 artifact/detail Pydantic models,
- deterministic artifact UID helpers,
- IPSW/OTA converters,
- v2 GCS prefix store skeleton,
- local parity report from existing legacy metadata,
- metrics/log structure for comparing v1/v2 read and write paths.

Do not mutate `gs://apple_symbols/` until explicitly confirmed for a concrete simulation run.
