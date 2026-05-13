# Iteration 006: scope boundaries and metadata-source routing

Date: 2026-05-12

## What changed

The metadata-v2 scope was clarified before adding GCS bootstrap behavior.

Explicit non-goals:

- changing mirror object storage layout or mirror lifecycle,
- changing symbol-storage layout,
- changing the symbolicator-facing symbol store interface,
- migrating/rekeying existing symbol-store data.

`artifact_uid` is metadata identity. It may or may not ever become the symsorter bundle ID for future uploads, but it cannot be assumed to be the bundle ID for all artifacts.

The v2 artifact model now records the symbolicator-facing identity actually used for each artifact as metadata:

```text
symbol_store_prefix
symbol_bundle_id
```

This is for parity/debugging and to keep the distinction explicit. If a future design chooses to use `artifact_uid` as the bundle ID for new uploads, the symbol store will intentionally contain both legacy bundle IDs and artifact-UID-based bundle IDs because existing symbol-store data will not be migrated.

A new symbolicator-facing storage interface is out of scope. Separate read-optimized debugging materializations are not ruled out for later design work, for example answering questions such as "which symbols exist for this debug ID", "resolve this offset for this debug ID", or "find `func_X` across fundamental frameworks for iOS 16". Those would be Symx/debugging features, not symbolicator contract changes.

The v2 artifact model also now records the metadata source that produced the artifact record:

```text
metadata_source
```

Initial metadata sources:

```text
appledb
apple_ota_feed
runner_sim_cache
xcode_sim
```

This keeps `kind` and `metadata_source` separate:

- `kind` says what processing/extraction domain the artifact belongs to (`ipsw`, `ota`, `sim`),
- `metadata_source` says which upstream input model/importer produced the artifact record,
- `metadata_source` can route to source-specific detail schemas and update semantics,
- `source_url` can be absent when a source listing exposes a stable identity but not a stable raw URL.

That makes it possible to model future changes such as:

- OTA artifacts sourced from AppleDB instead of, or alongside, the current OTA feed via `ipsw`,
- simulator image/package artifacts sourced from whatever backs `ipsw download xcode --sim`,
- current simulator artifacts sourced from runner-local caches.

## Files changed

```text
symx/artifacts/model.py
symx/artifacts/convert.py
tests/test_artifacts_v2.py
docs/meta-v2/000-working-thesis.md
docs/meta-v2/002-gcs-bucket-and-storage-invariants.md
docs/meta-v2/006-scope-and-source-provenance.md
```

## Commands run

```sh
uv run pytest tests/test_artifacts_v2.py
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest
```

Final full-suite result:

```text
172 passed, 3 skipped
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

The artifact-v2 tests now assert that:

- IPSW converted records use `metadata_source = appledb`,
- OTA converted records use `metadata_source = apple_ota_feed`,
- IPSW converted records preserve the existing symbol store prefix and generated symsorter bundle ID,
- OTA converted records preserve the existing symbol store prefix and generated symsorter bundle ID,
- current converted symbol bundle IDs are distinct from metadata artifact UIDs.

## Mismatches or surprises

No runtime mismatches; this was a scope/modeling clarification.

## Changes to the production migration plan

The migration is now explicitly metadata-only with respect to mirrors and symbols.

Future GCS bootstrap/shadow commands should only write metadata experiment objects under their configured prefix. They should not attempt to reorganize or rewrite mirror/symbol objects. If a later experiment considers `artifact_uid` as the symsorter bundle ID for new uploads, it must be modeled as a forward-only choice with mixed bundle-id forms in the symbol store.

Metadata-source information should be included in real bucket reports so we can evaluate whether AppleDB-as-OTA-source or `ipsw download xcode --sim` simulator package metadata are plausible future inputs without changing extraction/storage behavior.

## Next proposed iteration

Proceed with a v2 GCS prefix store and bootstrap command that reads legacy root metadata and writes normalized metadata under an experiment prefix only.
