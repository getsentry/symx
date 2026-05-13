# Iteration 004: normalize on Pydantic models

Date: 2026-05-12

## What changed

The migration thesis now includes a modeling hygiene goal: use Pydantic models consistently for Symx data contracts instead of mixing Pydantic domain models with dataclasses in newer admin/projection code.

The immediate motivation is not that dataclasses are wrong. They are lightweight and understandable for local-only helper rows, and they were a reasonable offset for admin snapshot work. But the migration is about making metadata contracts explicit and cross-checkable, and mixed modeling systems increase friction:

- different serialization/deserialization paths,
- different validation behavior,
- different defaults and coercion semantics,
- less uniform JSON/schema/documentation support,
- more ad-hoc conversion code between admin, stats, storage, and domain paths.

## Commands run

No application commands were run.

Documentation was added in the meta-v2 worktree:

```text
docs/meta-v2/004-pydantic-model-normalization.md
```

## GCS access

None.

## Prefixes written

None.

## Proposed rule

For metadata v2 and any cross-boundary data contract, prefer Pydantic `BaseModel`.

This includes:

- canonical artifact records,
- domain detail records,
- manifests,
- parity reports,
- transition/event records,
- admin apply request/result payloads,
- snapshot info that is serialized or shared across commands/workflows,
- read projection rows when they are passed across module boundaries.

Dataclasses may still be acceptable for strictly local implementation details that are never serialized, persisted, or shared across CLI/admin/workflow boundaries. But the default for the migration should be Pydantic.

## Migration approach

Do not stop the metadata migration to rewrite all admin dataclasses immediately.

Instead:

1. New meta-v2 code uses Pydantic from the start.
2. Any admin/stats type touched for v2 parity work should be converted opportunistically if it represents a persisted or serialized contract.
3. Compatibility adapters can keep existing function signatures temporarily.
4. Once normalized snapshots/projections exist, convert admin snapshot rows and report payloads to Pydantic in focused changes.

Likely candidates to convert over time:

- `SnapshotManifest`
- `SnapshotPaths` only if it becomes a serialized/config boundary; otherwise it can remain a local helper
- `SnapshotInfo`
- admin row models used by TUI/actions
- admin apply request/result models if not already Pydantic
- coverage report row/payload models

## Production migration impact

Using one modeling system should make the shadow migration easier to validate:

- same model validation for local and GCS-backed data,
- easier JSON round-trip testing,
- clearer schema evolution via `schema_version`,
- less ambiguity when comparing v1-derived and v2-derived projections.

## Next proposed iteration

When implementing the first code slice, define all new meta-v2 records as Pydantic models and add JSON round-trip tests for them.
