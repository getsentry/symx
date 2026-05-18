# Iteration 019: SQLite upload and update simulation

Date: 2026-05-12

## What changed

Added commands to upload compressed SQLite metadata candidates and simulate generation-matched state updates against them.

New upload command:

```sh
uv run symx artifacts sqlite upload \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-sqlite/001 \
  --input-dir /tmp/symx-sqlite-meta-001
```

New update simulation command:

```sh
uv run symx artifacts sqlite simulate-update \
  --storage gs://apple_symbols/ \
  --object-name experiments/meta-sqlite/001/ota.sqlite.gz
```

The simulation:

1. reads GCS generation,
2. downloads the compressed SQLite object,
3. decompresses it locally,
4. updates one artifact row's `processing_state` to `ignored`,
5. runs `PRAGMA integrity_check`,
6. recompresses,
7. uploads with `if_generation_match=<original_generation>`.

This is intentionally a mutation against the experiment prefix only.

## Files changed

```text
symx/artifacts/sqlite_store.py
symx/artifacts/app/__init__.py
tests/test_artifacts_sqlite.py
tests/test_artifacts_storage.py
docs/meta-v2/019-sqlite-upload-and-update-simulation.md
```

## Commands run

Upload compressed SQLite candidates:

```sh
uv run symx artifacts sqlite upload \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-sqlite/001 \
  --input-dir /tmp/symx-sqlite-meta-001
```

Output:

```json
{
  "prefix": "experiments/meta-sqlite/001",
  "storage": "gs://apple_symbols/",
  "uploaded_objects": [
    "experiments/meta-sqlite/001/metadata.sqlite.gz",
    "experiments/meta-sqlite/001/ipsw.sqlite.gz",
    "experiments/meta-sqlite/001/ota.sqlite.gz"
  ]
}
```

Inspect uploaded sizes:

```sh
gcloud storage ls -l gs://apple_symbols/experiments/meta-sqlite/001/*.sqlite.gz
```

Output:

```text
   4029361  gs://apple_symbols/experiments/meta-sqlite/001/ipsw.sqlite.gz
  18201255  gs://apple_symbols/experiments/meta-sqlite/001/metadata.sqlite.gz
  11933849  gs://apple_symbols/experiments/meta-sqlite/001/ota.sqlite.gz
TOTAL: 3 objects, 34164465 bytes (32.58MiB)
```

OTA update simulation:

```sh
/usr/bin/time -p uv run symx artifacts sqlite simulate-update \
  --storage gs://apple_symbols/ \
  --object-name experiments/meta-sqlite/001/ota.sqlite.gz
```

Output:

```json
{
  "artifact_uid": "ota:0001fd0dead734fb6744b1e765aeb7952bb466168e20bae88604b04b07020266",
  "compress_seconds": 0.503,
  "compressed_size_after": 11933907,
  "compressed_size_before": 11933849,
  "decompress_seconds": 0.061,
  "download_seconds": 0.854,
  "integrity_check": "ok",
  "new_state": "ignored",
  "previous_state": "indexed_duplicate",
  "total_seconds": 3.557,
  "update_seconds": 0.147,
  "upload_seconds": 1.916
}
```

Shell timing:

```text
real 4.55
```

IPSW update simulation:

```sh
/usr/bin/time -p uv run symx artifacts sqlite simulate-update \
  --storage gs://apple_symbols/ \
  --object-name experiments/meta-sqlite/001/ipsw.sqlite.gz
```

Output summary:

```json
{
  "compressed_size_before": 4029361,
  "compressed_size_after": 4029382,
  "download_seconds": 0.79,
  "decompress_seconds": 0.025,
  "update_seconds": 0.042,
  "compress_seconds": 0.172,
  "upload_seconds": 2.086,
  "total_seconds": 3.205,
  "integrity_check": "ok"
}
```

Shell timing:

```text
real 4.21
```

Combined metadata update simulation:

```sh
/usr/bin/time -p uv run symx artifacts sqlite simulate-update \
  --storage gs://apple_symbols/ \
  --object-name experiments/meta-sqlite/001/metadata.sqlite.gz
```

Output summary:

```json
{
  "compressed_size_before": 18201255,
  "compressed_size_after": 18201242,
  "download_seconds": 1.065,
  "decompress_seconds": 0.08,
  "update_seconds": 0.183,
  "compress_seconds": 0.746,
  "upload_seconds": 2.786,
  "total_seconds": 4.95,
  "integrity_check": "ok"
}
```

Shell timing:

```text
real 6.05
```

Full verification:

```sh
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest
```

Final full-suite result:

```text
179 passed, 3 skipped
```

## GCS access

Wrote under:

```text
gs://apple_symbols/experiments/meta-sqlite/001/
```

Updated, via generation match, these experiment objects:

```text
experiments/meta-sqlite/001/ipsw.sqlite.gz
experiments/meta-sqlite/001/ota.sqlite.gz
experiments/meta-sqlite/001/metadata.sqlite.gz
```

No production/root metadata, mirror objects, or symbol objects were modified.

## Prefixes written

```text
experiments/meta-sqlite/001/metadata.sqlite.gz
experiments/meta-sqlite/001/ipsw.sqlite.gz
experiments/meta-sqlite/001/ota.sqlite.gz
```

## Validation results

All update simulations completed with:

```text
PRAGMA integrity_check = ok
```

Generation numbers changed after each upload, confirming generation-matched writes succeeded.

## Mismatches or surprises

No semantic mismatches.

The update costs are small compared with artifact processing timescales:

- per-domain SQLite update: about 3.2-3.6s measured inside the command, about 4.2-4.6s shell wall time,
- combined SQLite update: about 5.0s measured inside the command, about 6.0s shell wall time.

This is strongly favorable to the SQLite-blob candidate. It suggests whole-object SQLite CAS may be entirely acceptable for this workload.

## Changes to the production migration plan

SQLite blob should now be considered the leading candidate over object-per-artifact metadata unless a later experiment reveals unacceptable conflict rates or update-path complexity.

The per-domain DB layout is especially attractive because it preserves current IPSW/OTA write-isolation while giving each domain a SQL schema.

## Next proposed iteration

Prototype a real domain update operation in SQLite form, not just a generic state update:

- OTA: select one `indexed` artifact, update it to `mirrored` with a mirror path,
- IPSW: select one `indexed` source-equivalent artifact, update it to `mirrored` with a mirror path,
- compare code clarity to current JSON update paths.
