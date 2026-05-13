# Iteration 017: SQLite blob as the core metadata store candidate

Date: 2026-05-12

## What changed

Added a new candidate architecture prompted by the v2 object-per-artifact cost accounting:

> Store the authoritative metadata as a compressed SQLite database object in GCS instead of two JSON files or one-object-per-artifact metadata.

This is not a remote SQL server. Writers still download the database, mutate it locally, and upload it back with a GCS generation precondition. But every workflow/admin/debug path gets a real SQL schema and indexes once the object is downloaded.

## Why this may be a sensible compromise

It keeps several useful properties of the current JSON design:

- one/few metadata objects,
- cheap aggregate reads,
- no need to list tens of thousands of metadata objects,
- no mandatory remote materializations,
- same optimistic concurrency pattern via GCS object generation,
- easy local admin/debug consumption.

It also gains many of the modeling benefits that motivated metadata-v2:

- normalized tables,
- source-specific detail tables,
- migrations with explicit schema versions,
- indexes for read/query paths,
- less ad-hoc JSON traversal,
- easier transition to a real SQL server later,
- a single modeling system for processing/admin/stats/debug projections.

## Trade-off vs object-per-artifact v2

Compared to object-per-artifact metadata, a SQLite blob does **not** reduce mutation blast radius at the GCS object level. The whole database object is still the optimistic-concurrency unit.

However, the experiments and workload shape suggest this may not be a practical problem. Mirroring/extraction work is measured in minutes and often moves gigabytes of payload data. Moving a compressed metadata DB that is likely far below 100MiB is small in comparison. The measured uncompressed snapshot download was about 5 seconds, while reading all object-per-artifact v2 metadata took more than 6 minutes even with concurrency.

So the relevant question is not "does SQLite blob have object-level contention?" It does. The question is whether that contention is meaningful compared with artifact processing duration and workflow scheduling. It may not be.

It avoids the largest costs observed in the object-per-artifact experiment:

- no 121k-object metadata materialization,
- no expensive full-object listing for aggregate reads,
- no required incremental admin cache just to avoid re-downloading every tiny object,
- fewer GCS API operations for full snapshots,
- simpler admin sync path.

## Trade-off vs current JSON

Compared to current JSON metadata, a SQLite blob keeps the same basic write-contention class but improves local structure:

- SQL updates can touch precise rows locally,
- constraints/indexes can enforce more invariants,
- migrations can add tables/columns without reshaping large Pydantic JSON documents everywhere,
- admin/stats/debug queries become ordinary SQL,
- future SQL-server migration becomes much more direct.

The remaining downside is the same as JSON: publishing a mutation uploads a whole metadata object and conflicts with unrelated writes to the same object. Based on current processing scale, this should be measured rather than assumed to be a blocker.

## Important design choice: one DB or multiple DBs

A single `metadata.sqlite.gz` maximizes unified querying and normalized modeling, but increases cross-domain write contention compared with today because IPSW and OTA currently live in separate JSON objects.

Given the actual processing timescale, that extra contention may still be acceptable. But a safer compromise is one SQLite DB per current write-isolation domain:

```text
meta/ipsw.sqlite.gz
meta/ota.sqlite.gz
meta/sim.sqlite.gz
```

Each DB can share the same normalized schema shape:

```sql
artifacts (...)
ipsw_details (...)
ota_details (...)
sim_details (...)
transition_events (...)
```

but only populate the relevant domain rows. Admin/debug tools can download and `ATTACH` multiple DBs locally for cross-domain views.

This preserves the current IPSW-vs-OTA write isolation while still moving the data model to SQL. A later real SQL-server migration can merge the domains into one database if that becomes useful.

## Writer algorithm sketch

For a DB object such as `meta/ota.sqlite.gz`:

1. load object metadata and remember GCS generation,
2. download the compressed SQLite DB,
3. decompress to a local temp file,
4. open SQLite locally,
5. perform reads/updates in a local transaction,
6. run lightweight validation/integrity checks,
7. close the DB cleanly,
8. compress to a temp object payload,
9. upload with `if_generation_match=<original_generation>`,
10. on precondition failure, download latest DB and retry/reapply the logical update.

This is very close to the current JSON read-modify-write model, but with a better local data model.

## SQLite publishing cautions

Do not upload a live WAL/journal set by accident.

Prefer a publish path like:

- use a local temp DB file,
- use `journal_mode=DELETE` or create a clean backup DB with SQLite backup/VACUUM INTO,
- close all connections,
- run `PRAGMA integrity_check`,
- compress the final single DB file,
- upload create/update with generation precondition.

The compressed object should probably be stored as an explicit `.sqlite.gz` (or similar) object, not as transparent HTTP content-encoding, so readers control decompression behavior.

## Open questions

- Is a single combined DB acceptable, or should we preserve current per-domain contention boundaries?
- Is gzip good enough, or should we add zstd for faster/smaller metadata objects?
- What is the measured compressed size of normalized per-domain DBs?
- How often do unrelated writes actually conflict today, measured against artifact processing runtimes?
- Can we express current IPSW and OTA update semantics cleanly in SQL migrations and row updates?
- Does row-level SQL clarity reduce enough code complexity to justify keeping whole-object CAS?
- Is whole-object CAS actually costly enough to matter when metadata transfer is seconds and artifact processing is minutes?

## Relationship to object-per-artifact experiment

The object-per-artifact experiment was still useful. It measured the real cost of maximizing GCS-native object-level metadata and exposed the aggregate-read trade-off.

The SQLite-blob candidate is a direct result of that cost accounting: it keeps SQL/modeling wins while avoiding the object-count/listing costs.

## Changes to the migration plan

This should be treated as a first-class alternative before pushing further on object-per-artifact metadata.

Next useful experiments:

1. Build a normalized SQLite DB locally from current JSON metadata.
2. Measure raw and compressed size.
3. Split into per-domain SQLite DBs and measure size/contention boundaries.
4. Implement one read-only report/query path over the SQLite DB.
5. Simulate one metadata update with GCS generation-matched upload against `gs://apple_symbols/`.
6. Compare code complexity against both current JSON and object-per-artifact v2.

## Commands run

No application commands were run for this documentation-only iteration.

## GCS access

None.

## Prefixes written

None.
