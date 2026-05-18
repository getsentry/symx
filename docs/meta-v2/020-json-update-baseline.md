# Iteration 020: legacy JSON update baseline

Date: 2026-05-12

## What changed

Added legacy JSON experiment commands and measured generation-matched update costs against JSON copies in the non-production bucket.

New commands:

```sh
uv run symx artifacts json upload-copies \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-json/001
```

```sh
uv run symx artifacts json simulate-update \
  --storage gs://apple_symbols/ \
  --object-name experiments/meta-json/001/ipsw_meta.json \
  --kind ipsw
```

```sh
uv run symx artifacts json simulate-update \
  --storage gs://apple_symbols/ \
  --object-name experiments/meta-json/001/ota_image_meta.json \
  --kind ota
```

The simulation:

1. reads GCS generation,
2. downloads the JSON object,
3. parses and updates one artifact/source state to `ignored`,
4. serializes the full JSON payload,
5. uploads with `if_generation_match=<original_generation>`.

Only experiment-prefix copies were mutated.

## Files changed

```text
symx/artifacts/json_store.py
symx/artifacts/app/__init__.py
tests/test_artifacts_json_store.py
docs/meta-v2/020-json-update-baseline.md
```

## Commands run

Upload JSON copies:

```sh
uv run symx artifacts json upload-copies \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-json/001
```

Output:

```json
{
  "prefix": "experiments/meta-json/001",
  "storage": "gs://apple_symbols/",
  "uploaded_objects": [
    "experiments/meta-json/001/ipsw_meta.json",
    "experiments/meta-json/001/ota_image_meta.json"
  ]
}
```

Object sizes:

```sh
gcloud storage ls -l gs://apple_symbols/experiments/meta-json/001/*.json
```

Output:

```text
   7360619  gs://apple_symbols/experiments/meta-json/001/ipsw_meta.json
  28964249  gs://apple_symbols/experiments/meta-json/001/ota_image_meta.json
TOTAL: 2 objects, 36324868 bytes (34.64MiB)
```

IPSW JSON update:

```sh
/usr/bin/time -p uv run symx artifacts json simulate-update \
  --storage gs://apple_symbols/ \
  --object-name experiments/meta-json/001/ipsw_meta.json \
  --kind ipsw
```

Output:

```json
{
  "download_seconds": 0.783,
  "kind": "ipsw",
  "new_state": "ignored",
  "previous_state": "indexed",
  "size_after": 7360619,
  "size_before": 7360619,
  "total_seconds": 2.396,
  "update_seconds": 0.098,
  "upload_seconds": 1.426
}
```

Shell timing:

```text
real 3.46
```

OTA JSON update:

```sh
/usr/bin/time -p uv run symx artifacts json simulate-update \
  --storage gs://apple_symbols/ \
  --object-name experiments/meta-json/001/ota_image_meta.json \
  --kind ota
```

Output:

```json
{
  "download_seconds": 1.507,
  "kind": "ota",
  "new_state": "ignored",
  "previous_state": "symbol_extraction_failed",
  "size_after": 28964232,
  "size_before": 28964249,
  "total_seconds": 6.022,
  "update_seconds": 0.292,
  "upload_seconds": 4.125
}
```

Shell timing:

```text
real 7.09
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
182 passed, 3 skipped
```

## GCS access

Copied root JSON metadata to:

```text
gs://apple_symbols/experiments/meta-json/001/ipsw_meta.json
gs://apple_symbols/experiments/meta-json/001/ota_image_meta.json
```

Then updated those experiment copies via generation-matched writes.

No root metadata, mirror objects, or symbol objects were modified.

## Prefixes written

```text
experiments/meta-json/001/ipsw_meta.json
experiments/meta-json/001/ota_image_meta.json
```

## Validation results

Both JSON update simulations succeeded and changed GCS generations.

## Comparison with SQLite experiment

Measured command-internal update times:

| Store object | Size before | Total seconds |
| --- | ---: | ---: |
| JSON IPSW | 7.36 MB | 2.396s |
| SQLite IPSW gzip | 4.03 MB | 3.205s |
| JSON OTA | 28.96 MB | 6.022s |
| SQLite OTA gzip | 11.93 MB | 3.557s |
| SQLite combined gzip | 18.20 MB | 4.950s |

Measured shell wall times:

| Store object | Wall time |
| --- | ---: |
| JSON IPSW | 3.46s |
| SQLite IPSW gzip | 4.21s |
| JSON OTA | 7.09s |
| SQLite OTA gzip | 4.55s |
| SQLite combined gzip | 6.05s |

SQLite is not categorically faster for every object because it pays decompression/recompression overhead. For OTA and combined metadata, the smaller compressed transfer wins. For IPSW, current JSON is already small enough that uncompressed JSON update was slightly faster.

## Mismatches or surprises

The JSON baseline is already quite good. This reinforces that the main SQLite argument is not raw update speed alone.

SQLite's value is:

- normalized schema,
- SQL query ergonomics,
- local constraints/indexes,
- easier schema evolution,
- future SQL-server migration path,
- good-enough update performance.

The current JSON store remains a strong baseline for raw update cost.

## Changes to the production migration plan

Do not justify SQLite primarily as a faster update transport. Justify it, if at all, as a better data model and operational/query surface with comparable update costs.

The per-domain SQLite layout remains attractive because:

- OTA updates appear faster than current OTA JSON in this experiment,
- IPSW updates are close enough to current JSON,
- current IPSW/OTA write-isolation boundaries are preserved.

## Next proposed iteration

Prototype one real domain update path in SQLite form and compare code complexity to current JSON storage updates.
