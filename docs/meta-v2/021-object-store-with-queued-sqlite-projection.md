# Iteration 021: object store source of truth with queued SQLite projection

Date: 2026-05-12

## What changed

Recorded a fuller hybrid architecture candidate:

- normalized object-per-artifact/detail metadata is the authoritative live store,
- all workflow mutations write only those source-of-truth objects,
- non-workflow readers use a lagging SQLite projection,
- every changed source object is referenced by a GCS sync queue,
- a queue flusher updates the SQLite projection,
- admin/stats can trigger a flush before downloading the projection,
- a cron workflow can also flush periodically.

This is the "best of both worlds" version of object-per-artifact metadata: per-object CAS for workflow writes, plus a SQL read model for admin/stats without making every reader list/download all artifact objects.

## Proposed layout

Canonical live metadata:

```text
meta/artifacts/<artifact_uid>.json
meta/details/<metadata_source>/<artifact_uid>.json
```

Projection queue:

```text
meta/projection-queue/<encoded-object-name>.json
```

SQLite projection:

```text
meta/projections/admin.sqlite.gz
```

Names are illustrative. The important distinction is source-of-truth objects vs derived projection state.

## Queue item model

Use a coalescing queue item per changed source object, not necessarily append-only one item per update.

Queue item sketch:

```json
{
  "schema_version": 1,
  "object_name": "meta/artifacts/ipsw:...json",
  "object_kind": "artifact",
  "object_generation": 123456789,
  "artifact_uid": "ipsw:...",
  "queued_at": "...",
  "writer_run_id": 123
}
```

The queue object itself is updated with GCS generation preconditions. If the same source object changes again while a projector is processing an older queue item, the writer updates the queue item to the newer source object generation. The projector deletes the queue item only with the queue object's generation it read. If the queue item was updated concurrently, the delete fails and the newer work remains queued.

This matches the requirement that queue items themselves use generations to avoid losing updates when the same object changes multiple times during a flush.

## Write path

For a workflow mutation:

1. read source artifact object and generation,
2. update artifact/detail object with `if_generation_match`,
3. after successful source write, upsert the projection-queue item with the written source object generation,
4. workflow correctness depends only on the source object write, not on the projection being current.

The queue enqueue is not transactionally atomic with the source object write. Therefore the queue should be treated as an accelerator, not the only correctness mechanism. Writers should retry/report enqueue failures, and a periodic reconciliation scan should be able to discover source objects whose generation is newer than the projection row.

## Projection flush path

A flusher:

1. downloads current `admin.sqlite.gz` and remembers its GCS generation,
2. lists queue items,
3. for each queue item, downloads the referenced source object at or after the queued generation,
4. upserts the corresponding SQLite row and stores the source object generation in the row,
5. runs integrity checks,
6. uploads the updated SQLite projection with generation match,
7. deletes processed queue items with queue-generation match,
8. leaves queue items in place if upload/delete races occur.

If another flusher wins the SQLite projection upload, this flusher's upload fails and it must leave queue items untouched or retry from the new projection generation.

## Reader path

Admin/stats readers can choose:

- download the current lagging projection directly, or
- trigger/wait for a queue flush, then download the projection.

Admin writes must never write to the projection. They use the projection as a read source, then mutate source-of-truth artifact/detail objects with expected source object generations from the projection. If the source object generation changed, the admin mutation fails and the operator resyncs/retries.

## Reconciliation path

Because source-object update and queue-item update are not atomic, there must be a repair path.

Options:

- periodic full or partial scan comparing GCS source object generations to projection row generations,
- workflow-level retry/alert if enqueue fails,
- admin-triggered "repair projection" command if stale data is suspected.

Without reconciliation, a source object could be updated successfully but never projected if the writer dies after the source write and before queue enqueue.

## Strengths

- Very small workflow mutation RMW cycles.
- Per-artifact/detail CAS and low write contention.
- Admin/stats get SQL reads.
- Projection staleness is acceptable and explicit.
- Admin mutations can still validate source object generations.
- Queue coalescing avoids unbounded one-item-per-update growth for hot objects.
- Projection can be rebuilt if it becomes corrupt because source objects remain authoritative.

## Costs and risks

This is significantly more complex than both current JSON and SQLite-blob metadata:

- source object store,
- queue item model,
- queue coalescing CAS,
- projector workflow,
- projection upload CAS,
- queue item deletion/race handling,
- reconciliation for missed queue writes,
- projection schema/migrations,
- admin read/write split.

It solves aggregate-read cost but reintroduces a derived-data synchronization system. The SQLite projection is no longer the source of truth, so every writer/reader must respect the source/projection split.

## Comparison to SQLite blob candidate

The SQLite-blob candidate is much simpler and measured update costs are already in the same ballpark as JSON:

- IPSW SQLite update: about 4.2s shell wall time,
- OTA SQLite update: about 4.6s shell wall time,
- combined SQLite update: about 6.0s shell wall time.

The hybrid object/projection model is only worth considering if per-object live metadata updates are valuable enough to justify queue/projection/reconciliation complexity.

## Open questions

- Is the current whole-object metadata contention actually painful enough to justify this architecture?
- Should queue items be coalescing per source object, append-only per source generation, or both?
- Should the projection be global or per consumer (`admin`, `coverage`, etc.)?
- Should projection flush happen in GHA only, or also from local admin with GCS credentials?
- What is the acceptable reconciliation frequency for missed queue writes?
- Can we keep the writer API simple enough that queue enqueue failures are handled consistently?

## Suggested next experiment if pursued

Prototype only the queue/projection update mechanics, not the full migration:

1. take the existing object-per-artifact experiment prefix,
2. create a small projection DB from a subset of artifacts,
3. update one artifact object,
4. write/update one queue item,
5. flush queue into projection,
6. update same artifact again during/after flush and verify queue-generation delete safety.

## Current recommendation

Keep this as a valid architecture candidate, but do not assume it is the next implementation step.

Given the measured SQLite-blob update costs, the simpler SQLite-blob approach remains the leading practical compromise unless we can demonstrate that current whole-object contention is a real operational problem.
