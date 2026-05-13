# Iteration 018: SQLite core-store prototype

Date: 2026-05-12

## What changed

Added a prototype command that builds compressed SQLite metadata-store candidates from the current legacy metadata.

New command:

```sh
uv run symx artifacts sqlite build \
  --storage gs://apple_symbols/ \
  --output-dir /tmp/symx-sqlite-meta-001
```

It produces:

```text
metadata.sqlite
metadata.sqlite.gz
ipsw.sqlite
ipsw.sqlite.gz
ota.sqlite
ota.sqlite.gz
```

The combined DB contains both IPSW and OTA rows. The per-domain DBs preserve current write-isolation boundaries as a candidate compromise.

## Files changed

```text
symx/artifacts/sqlite_store.py
symx/artifacts/app/__init__.py
tests/test_artifacts_sqlite.py
docs/meta-v2/018-sqlite-core-store-prototype.md
```

## Commands run

Targeted implementation checks:

```sh
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest tests/test_artifacts_sqlite.py
```

Build from real metadata:

```sh
rm -rf /tmp/symx-sqlite-meta-001
/usr/bin/time -p uv run symx artifacts sqlite build \
  --storage gs://apple_symbols/ \
  --output-dir /tmp/symx-sqlite-meta-001
```

Query test:

```sh
/usr/bin/time -p sqlite3 /tmp/symx-sqlite-meta-001/metadata.sqlite \
  "select kind, processing_state, count(*) from artifacts group by kind, processing_state order by kind, count(*) desc;"
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
178 passed, 3 skipped
```

## GCS access

Read-only.

Read from:

```text
gs://apple_symbols/ipsw_meta.json
gs://apple_symbols/ota_image_meta.json
```

No GCS objects were written.

## Prefixes written

None.

Local outputs:

```text
/tmp/symx-sqlite-meta-001/metadata.sqlite
/tmp/symx-sqlite-meta-001/metadata.sqlite.gz
/tmp/symx-sqlite-meta-001/ipsw.sqlite
/tmp/symx-sqlite-meta-001/ipsw.sqlite.gz
/tmp/symx-sqlite-meta-001/ota.sqlite
/tmp/symx-sqlite-meta-001/ota.sqlite.gz
```

## Validation results

Build output:

```json
{
  "parity_ok": true,
  "parity_mismatch_count": 0,
  "dbs": [
    {
      "name": "metadata",
      "artifact_count": 60704,
      "raw_size_bytes": 89214976,
      "compressed_size_bytes": 18201255,
      "build_seconds": 0.763,
      "compress_seconds": 0.759,
      "integrity_check": "ok"
    },
    {
      "name": "ipsw",
      "artifact_count": 15569,
      "raw_size_bytes": 23105536,
      "compressed_size_bytes": 4029361,
      "build_seconds": 0.165,
      "compress_seconds": 0.172,
      "integrity_check": "ok"
    },
    {
      "name": "ota",
      "artifact_count": 45135,
      "raw_size_bytes": 66166784,
      "compressed_size_bytes": 11933849,
      "build_seconds": 0.61,
      "compress_seconds": 0.514,
      "integrity_check": "ok"
    }
  ]
}
```

End-to-end command timing:

```text
real 8.74
user 4.72
sys 0.82
```

Local file sizes:

```text
metadata.sqlite     86M
metadata.sqlite.gz  18M
ipsw.sqlite         23M
ipsw.sqlite.gz      3.9M
ota.sqlite          64M
ota.sqlite.gz       12M
```

Query timing for grouped processing-state counts:

```text
real 0.04
```

Query output:

```text
ipsw|indexed|14277
ipsw|mirror_corrupt|1240
ipsw|mirroring_failed|42
ipsw|symbols_extracted|9
ipsw|mirrored|1
ota|indexed_duplicate|27170
ota|symbol_extraction_failed|12017
ota|symbols_extracted|3347
ota|indexed|2586
ota|mirrored|7
ota|indexed_invalid|5
ota|delta_ota|3
```

## Mismatches or surprises

No semantic mismatches.

The size and timing results are strongly favorable compared with object-per-artifact metadata for aggregate reads:

- combined compressed SQLite metadata is about 18MiB,
- per-domain compressed DBs are about 3.9MiB and 12MiB,
- local SQL queries are effectively instant for admin/stats-style aggregations,
- building all DBs from current JSON took under 9 seconds locally.

This does not test write/update contention yet, but it makes the SQLite-blob candidate much more credible.

## Changes to the production migration plan

SQLite blob should be treated as the leading compromise candidate until update simulations prove otherwise.

The next meaningful comparison is no longer object-per-artifact read cost; it is SQLite object update cost:

1. upload the compressed SQLite candidates under an experiment prefix,
2. simulate one IPSW or OTA state update by downloading/decompressing/mutating/recompressing/uploading with generation match,
3. measure wall-clock time and retry behavior,
4. compare code complexity with current JSON update paths.

## Next proposed iteration

Add an upload command for the generated SQLite DBs using create-only writes, then run a state-update simulation against the experiment copy.
