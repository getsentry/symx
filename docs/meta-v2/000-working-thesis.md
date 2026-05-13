# Symx metadata v2 working thesis

Status: working document for an incremental, non-destructive metadata-v2 migration.

This document is intentionally not a final architecture spec. After each simulation or implementation iteration, add a new document in this directory that records what we learned and how the production migration path changed.

## Goal

Build a normalized artifact metadata layer while still operating within the current constraints:

- Google Cloud Storage is the only shared persistent backend.
- GitHub Actions runners remain the worker nodes.
- Existing IPSW/OTA production workflows must continue to work during the migration.
- Artifact processing states remain **resting states**. In-flight execution is represented by GHA runs/logs/Sentry today, and possibly lease objects later, but not by adding `running` artifact states.

The target normalized artifact is the downloadable/storable/processable file plus storage/state metadata. Domain-specific ingestion/update details remain domain-specific.

## Explicit non-goals

This investigation is about metadata structure and metadata update semantics.

It explicitly does **not** investigate changing:

- mirror object storage layout or mirror object lifecycle,
- symbol-storage layout,
- the symbolicator-facing symbol store interface,
- migrating/rekeying existing symbol-store data.

`artifact_uid` is a metadata identity. The normalized metadata records the actual symbol-store prefix and bundle ID used for a given artifact so existing symbols can still be related back to metadata.

Changing future symsorter bundle IDs to use `artifact_uid` is not ruled out as a design option, but it would be a forward-only behavior change. Existing symbol-store data must not be migrated, so choosing that option means the symbol store will permanently contain both legacy bundle IDs and artifact-UID-based bundle IDs. Any metadata/debug tooling must model the bundle ID that was actually used for each artifact instead of assuming a single derivation rule.

A new symbolicator-facing symbol-store interface is out of scope. However, read-optimized metadata/symbol debugging materializations are not ruled out as future experiments, as long as they do not change symbolicator's contract.

## Current thesis

The current IPSW and OTA metadata stores can coexist with a v2 artifact store for a long time.

A key read-path principle: the canonical store should not require global materialized snapshots by default. Consumers that need queryable aggregate data can maintain consumer-owned local caches/projections. For example, admin sync can build a local SQLite cache from artifact/detail objects and then update only remote objects whose GCS generation changed.

The useful migration shape is not a delayed, offline-only model replacement. It is:

1. Keep v1 production-authoritative initially.
2. Introduce v2 as a **shadow store from the first iteration**, not as a later side project.
3. Put v1 and v2 behind the same storage-facing seams wherever practical, so current operations can be run through comparable read/write paths.
4. Duplicate updates into v2 behind the facade while v1 remains authoritative.
5. Reload/cross-check v1 and v2 after representative reads and writes.
6. Collect enough evidence about correctness, complexity, conflict behavior, and performance to decide whether to do a full metadata cutover or a staged cutover with explicit safe points.
7. Keep v1 compatibility available as long as rollback/replay or legacy consumers need it.

Important caveat: both storage mechanisms can live in parallel, but a given mutation path should still have one clear authority at a time. Initially, v1 commits decide production behavior; v2 shadow writes are observations/materializations of the same domain mutation. Long-term dual-authoritative writes are a divergence trap unless there is a deliberate reconciliation protocol.

## Test bucket and safety rules

Simulation target proposed by the user:

```text
gs://apple_symbols/
```

This is a personal, non-production bucket. Experimentation there is acceptable, but it should still be treated as useful state: making it inconsistent for no reason costs upload/download time to recreate and makes simulation results less valuable.

Initial simulation rules:

- Mutating commands against the bucket are allowed for concrete simulation steps after we agree on what is being tested.
- Do not casually overwrite or remove expensive existing state.
- Do not write to root production-like object names by default unless the iteration explicitly requires testing legacy-compatible behavior:
  - `ipsw_meta.json`
  - `ota_image_meta.json`
  - `mirror/...`
  - `symbols/...`
- Initial v2 simulation writes should be namespaced, for example:

```text
experiments/meta-v2/<run-id>/artifacts/...
experiments/meta-v2/<run-id>/details/...
experiments/meta-v2/<run-id>/views/...
experiments/meta-v2/<run-id>/reports/...
```

`v2` is the migration/project name, not necessarily a durable storage namespace. After the simulation path is proven, the canonical metadata layout should use unversioned production prefixes:

```text
meta/...
views/...
```

Schema evolution should be handled with object `schema_version` fields, additive detail/projection objects, and explicit migrations rather than by keeping a permanent `v2` path in production.

## Normalized artifact record

The v2 core record should contain fields common to storage, mirroring, file identity, and processing state.

Sketch:

```json
{
  "schema_version": 1,
  "artifact_uid": "ipsw:<stable-id>",
  "kind": "ipsw",

  "platform": "iOS",
  "version": "18.2",
  "build": "22C152",
  "release_status": "rel",
  "released_at": "2024-12-11",

  "metadata_source": "appledb",
  "source_url": "https://...",
  "source_key": "legacy-source-identity",
  "filename": "...ipsw",
  "size_bytes": 123456,
  "hash_algorithm": "sha1",
  "hash_value": "...",

  "mirror_path": "mirror/ipsw/...",
  "processing_state": "mirrored",

  "symbol_store_prefix": "ios",
  "symbol_bundle_id": "ipsw_...",

  "last_run": 123456789,
  "last_modified": "2026-05-12T12:00:00Z",

  "detail_path": ".../details/ipsw/<artifact_uid>.json",
  "legacy": {
    "store": "ipsw",
    "artifact_key": "iOS_18.2_22C152",
    "source_link": "https://..."
  }
}
```

The core artifact record owns:

- artifact UID,
- kind/domain,
- platform/version/build,
- metadata source plus source URL/key; `source_url` may be absent for source listings that expose an identity but not a stable URL,
- filename,
- hash/size,
- mirror path pointer,
- resting processing state,
- existing symbol-store prefix/bundle-id pointer,
- last mutation/run metadata.

Domain-specific detail objects own source interpretation:

- IPSW: AppleDB grouping, release hierarchy, devices, source index/link, raw/source fields.
- OTA: OTA key/id, description, duplicate classification inputs, devices, raw feed fields. Current OTA metadata comes from Apple's OTA feed via `ipsw`, but the model should allow AppleDB to become an OTA source later.
- Simulator: runtime/package/cache identity, host image, Xcode/macOS context, arch. The model should allow both current runner-local simulator cache discovery and future simulator image/package metadata sourced from whatever backs `ipsw download xcode --sim`.

## Proposed GCS layout

Simulation layout:

```text
experiments/meta-v2/<run-id>/
  artifacts/<artifact_uid>.json
  details/ipsw/<artifact_uid>.json
  details/ota/<artifact_uid>.json
  details/sim/<artifact_uid>.json
  manifests/bootstrap.json
  reports/parity.json
  views/snapshot.db
```

Canonical layout, once proven:

```text
meta/artifacts/<artifact_uid>.json
meta/details/ipsw/<artifact_uid>.json
meta/details/ota/<artifact_uid>.json
meta/details/sim/<artifact_uid>.json
meta/events/<yyyy>/<mm>/<dd>/<artifact_uid>/<event_id>.json
views/current.json
views/snapshots/<snapshot_id>/snapshot.db
```

Rationale: there is no existing `meta/v1` or `views/v1` interface, and the desired end state is simply the Symx metadata model rather than a permanently namespaced migration model. Keeping `v2` in canonical object names would mostly preserve implementation history in the storage layout.

Use object-level generation checks for artifact updates. This reduces the current full-JSON rewrite conflict surface without requiring SQL.

The key storage-safety thesis is that v2 maps better to GCS than the current monolithic metadata JSON files:

- GCS already provides object versions/generations and object-level conditional writes.
- A single artifact object can be updated with a much smaller contention surface than a full metadata database blob.
- Adding new metadata can often mean adding a new detail/projection object instead of changing one large shared schema and every reader at once.
- The amount of state that can be left inconsistent by one failed metadata write is smaller and easier to validate/repair.

This does not remove the need for careful storage code, but it should reduce the amount of fragile invariants every writer must preserve.

## Stable artifact IDs

The first implementation should use deterministic IDs and retain legacy back-references.

Initial candidate:

```text
ipsw:<sha256("ipsw\0" + ipsw_artifact_key + "\0" + source_url)>
ota:<sha256("ota\0" + ota_key)>
sim:<sha256("sim\0" + runtime_identity + "\0" + arch)>
```

Open question for iteration 1: whether OTA IDs should be based on the current OTA key, the zip id, or a composed identity that preserves duplicate semantics better.

## Consumers, facades, and cross-checking

There is no free migration path where consumers stay single-mode and we still learn enough. Any deployable, incremental migration needs some amount of double modeling:

- read consumers need a v1-derived result and a v2-derived result that can be compared,
- write consumers need an authoritative write path plus a shadow/materialized write path,
- admin/stats can be early consumers if they are explicitly wired for parity checks rather than silently switching source models.

The first useful consumer does not have to be admin/stats, but it also does not have to avoid them. The better rule is: whichever consumer is touched first must expose comparable v1/v2 behavior and must not make production behavior depend on v2 until the flag says so.

Suggested initial commands remain useful, but as baseline tooling rather than the whole first phase:

```text
symx artifacts v2 bootstrap --storage gs://apple_symbols --prefix experiments/meta-v2/<run-id>
symx artifacts v2 validate  --storage gs://apple_symbols --prefix experiments/meta-v2/<run-id>
symx artifacts v2 report    --storage gs://apple_symbols --prefix experiments/meta-v2/<run-id>
```

The bootstrap command reads legacy metadata, writes normalized v2 records under the simulation prefix, and writes a manifest.

The validate command reads both v1 and v2 and reports parity:

- artifact counts by kind,
- state counts by kind,
- mirror path equality,
- hash/size equality,
- missing required fields,
- duplicated artifact UIDs,
- legacy back-reference reversibility,
- current worklist parity for mirror/extract eligibility.

For operations behind the storage facade, validation should additionally compare:

- selected work item identity and ordering,
- expected state transition,
- persisted post-write state,
- CAS/generation retry counts,
- bytes read/written for metadata,
- object count touched per operation,
- wall-clock time for metadata read/update phases,
- divergence rate and reason.

The report command should produce useful data before production migration starts, for example:

- normalized artifact inventory,
- IPSW source count vs OTA artifact count,
- processing-state distribution,
- field completeness by kind,
- mirror coverage,
- likely identity collisions,
- duplicate OTA/IPSW hash groups if any.

## Feature-flag migration shape

Commands should support modes like:

```text
SYMX_META_MODE=v1
SYMX_META_MODE=v1-shadow-v2
SYMX_META_MODE=v1-shadow-v2-strict
SYMX_META_MODE=v2-validate-v1
SYMX_META_MODE=v2
```

Meaning:

- `v1`: current behavior only.
- `v1-shadow-v2`: v1 remains authoritative; successful v1 mutations also update or materialize v2; v2 failures are reported but do not change production behavior.
- `v1-shadow-v2-strict`: v1 remains authoritative, but v2 divergence fails the command in non-production or explicitly opted-in simulations.
- `v2-validate-v1`: command reads/plans from v2 but cross-checks expected v1 eligibility/state before mutating.
- `v2`: v2 is authoritative; v1 compatibility output is generated if still needed for rollback/replay or legacy consumers.

Cutover does not have to be an elaborate per-consumer egg dance if the shadow simulation proves the new path. The migration should support both outcomes:

- a one-time production migration plus full metadata behavior switch, if v2 shadowing demonstrates clear correctness and operational upside,
- or staged safe points per command/domain, if simulation reveals lifecycle-specific risks.

Possible evidence-gathering order:

1. v2 bootstrap/report/validate commands.
2. storage facade with `v1-shadow-v2` writes against the simulation prefix.
3. v2 worklist generation compared against current v1 workflow eligibility.
4. representative IPSW mirror/extract state transitions in shadow mode.
5. representative OTA meta/mirror/extract state transitions in shadow mode.
6. admin/stats parity paths once the normalized snapshot/projection is available.
7. a documented decision: full cutover vs staged cutover with safe points.

## Current-use mapping

| Current use          | v2 migration target                                                                                                       |
|----------------------|---------------------------------------------------------------------------------------------------------------------------|
| IPSW AppleDB sync    | IPSW importer emits or materializes artifact records plus IPSW details                                                    |
| IPSW mirror          | artifact mirror over `kind=ipsw`, preserving current recency eligibility                                                  |
| IPSW extract         | artifact extract over `kind=ipsw`, using IPSW detail to reconstruct extractor input                                       |
| OTA metadata refresh | OTA importer/merge emits or materializes artifact records plus OTA details                                                |
| OTA mirror           | artifact mirror over `kind=ota`                                                                                           |
| OTA extract          | artifact extract over `kind=ota`, preserving OTA-specific terminal states                                                 |
| Simulator extract    | simulator inventory creates artifacts; extraction becomes stateful once package/cache identity is stable                  |
| Admin sync           | can consume normalized snapshot projection once parity mode exists; not special-cased away from cross-checking            |
| Admin apply          | eventually CAS-patches artifact rows with expected generation/state; before cutover, shadow-applies comparable v2 updates |
| Coverage page        | can query normalized snapshot projection once v1/v2 coverage parity is checked                                            |
| Symbol store         | no migration/rekeying and no symbolicator interface changes; metadata records the bundle ID actually used                 |

## Model-system hygiene

The metadata-v2 migration should also normalize Symx data contracts on Pydantic models.

The existing admin code introduced several dataclasses. That is understandable for local snapshot/TUI work, but mixed modeling systems add friction during a metadata migration:

- different validation behavior,
- different JSON serialization paths,
- different default/coercion semantics,
- more adapter code between storage, admin, stats, and CLI boundaries.

Guidance:

- New canonical metadata, detail, event, manifest, report, and cross-command payload models should be Pydantic `BaseModel` types.
- Dataclasses may remain for strictly local implementation helpers that are not persisted, serialized, or shared across module/workflow boundaries.
- Admin/stats dataclasses should be converted opportunistically when touched for v2 parity or projection work, not as a blocking prerequisite for the metadata migration.

See [`004-pydantic-model-normalization.md`](004-pydantic-model-normalization.md).

## Aggregate read and admin-sync model

Object-per-artifact metadata is optimized for low-contention mutation, not for full aggregate scans. But a globally consistent snapshot is rarely required.

For admin usage, prefer a local incremental cache:

1. initial sync lists artifact/detail objects and downloads all rows,
2. the local SQLite cache stores the GCS object generation for each artifact/detail object,
3. later syncs list remote object metadata and download only objects whose generation is new or changed,
4. apply changed rows to the local SQLite cache in a transaction,
5. tolerate that the view is not a perfectly consistent point-in-time snapshot,
6. validate expected state/generation again before applying any admin mutation.

This preserves the main v2 benefit: no default store-level materialization that can go out of sync. Expensive aggregate work is paid by consumers that need it, and even those consumers can be incremental after the first sync.

## Production storage invariants

Regardless of v1 or v2, production storage code should not overwrite or remove data casually.

Important invariants:

- Mirrored artifacts are durable/replay inputs and should be treated as immutable once verified.
- Symbol output should remain additive/create-only until a deliberate replacement model exists.
- Metadata updates are the critical mutable path and must use conditional writes / optimistic concurrency.
- In v1, a single metadata write can affect the whole IPSW or OTA metadata blob.
- In v2, the same logical mutation should usually affect one artifact object plus optional event/projection objects, reducing contention and blast radius.
- Any compatibility materialization back to v1 JSON must be explicit about whether it is authoritative, generated, or diagnostic.

## Production migration document process

After every iteration, add a new document:

```text
docs/meta-v2/00N-<short-name>.md
```

Each iteration document should include:

1. what changed,
2. exact commands run,
3. whether GCS was read-only or mutated,
4. object prefixes written,
5. validation results,
6. mismatches or surprises,
7. changes to the production migration plan,
8. next proposed iteration.

## Iteration 1 proposal

No production behavior changes by default, but v2 should exist as a shadow path from the first useful implementation.

Implementation:

1. Add artifact v2 Pydantic models and JSON round-trip tests.
2. Add deterministic ID helpers.
3. Add converters from current IPSW/OTA models to v2 records.
4. Add a v2 GCS store for a configurable prefix.
5. Add an initial storage-facing facade or comparison harness that can run v1 reads plus v2 materialization/validation for the same operation.
6. Add CLI commands that can bootstrap, validate, and report v2 from existing legacy metadata.
7. Add lightweight metrics/log output for read/write comparison:
   - metadata bytes read/written,
   - number of GCS objects touched,
   - generation/CAS retries,
   - operation duration,
   - parity mismatch counts and classes.

Optional GCS step, only after confirmation:

1. Read legacy metadata from `gs://apple_symbols/`.
2. Write v2 simulation output under `experiments/meta-v2/<run-id>/` with create-only semantics.
3. Run comparable read/worklist operations against v1 and v2.
4. Run representative state-transition simulations under the v2 prefix without mutating root legacy metadata, mirrors, or symbols.
5. Write a parity/performance report.

Success criteria:

- generated v2 artifact count equals IPSW source count plus OTA artifact count,
- every v2 record maps back to exactly one legacy row,
- state counts match legacy exactly,
- mirror paths match legacy exactly,
- current mirror/extract worklists match legacy eligibility logic exactly,
- representative v1 state transitions can be expressed as v2 shadow updates,
- comparison metrics are good enough to decide whether a full cutover is plausible or staged safe points are necessary.
