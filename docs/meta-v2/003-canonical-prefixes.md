# Iteration 003: canonical prefixes should be unversioned

Date: 2026-05-12

## What changed

The working thesis no longer proposes permanent production prefixes named:

```text
meta/v2/...
views/v2/...
```

Instead, `v2` is treated as the migration/project name. The canonical production layout, once proven, should be unversioned:

```text
meta/artifacts/<artifact_uid>.json
meta/details/ipsw/<artifact_uid>.json
meta/details/ota/<artifact_uid>.json
meta/details/sim/<artifact_uid>.json
meta/events/<yyyy>/<mm>/<dd>/<artifact_uid>/<event_id>.json
views/current.json
views/snapshots/<snapshot_id>/snapshot.db
```

Simulation prefixes may still use `experiments/meta-v2/<run-id>/...` because that is an experiment namespace, not the final data model.

## Commands run

No application commands were run.

Documentation was edited in the meta-v2 worktree:

```text
docs/meta-v2/000-working-thesis.md
docs/meta-v2/003-canonical-prefixes.md
```

## GCS access

None.

No reads or writes were made against:

```text
gs://apple_symbols/
```

## Prefixes written

None.

## Reasoning

The original `meta/v2` / `views/v2` idea had some plausible motivations:

- isolate the new model while v1 JSON files still exist,
- make rollback easier during shadowing,
- leave room for future schema changes,
- make it visually obvious which objects belong to the migration.

But those reasons apply better to the simulation/experiment prefix than to the final production layout.

There is no existing `meta/v1` or `views/v1` storage interface. The current v1 metadata is represented by root objects like:

```text
ipsw_meta.json
ota_image_meta.json
```

Once the new model is accepted, it should simply be the Symx metadata model. Keeping `v2` in canonical object names would preserve migration history in the permanent storage layout and could imply a versioned public interface that does not really exist.

Schema evolution can instead be handled by:

- `schema_version` fields inside records,
- additive detail/projection objects,
- compatibility materializations where needed,
- explicit migrations for breaking changes.

GCS object generations/versioning address object mutation safety; they do not require schema-versioned prefixes.

## Changes to the production migration plan

The migration path now distinguishes three namespacing concepts:

1. existing legacy root metadata objects,
2. experiment/shadow prefixes, which may include `meta-v2` as a project label,
3. final canonical unversioned metadata/view prefixes.

The final cutover target is unversioned `meta/...` and `views/...`.

## Next proposed iteration

Implement code using configurable prefixes so the same store can target:

- `experiments/meta-v2/<run-id>/...` during simulation,
- `meta/...` and `views/...` after cutover.

Avoid baking `v2` into production object path helpers.
