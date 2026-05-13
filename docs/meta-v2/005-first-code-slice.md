# Iteration 005: first metadata-v2 code slice

Date: 2026-05-12

## What changed

Added the first executable metadata-v2 building blocks:

- normalized Pydantic artifact models,
- deterministic artifact UID helpers,
- converters from current IPSW/OTA metadata to normalized artifact bundles,
- local parity report generation,
- a new CLI entrypoint for local reports,
- tests for IDs, conversion, Pydantic JSON round-trips, and worklist parity.

New CLI:

```sh
uv run symx artifacts v2 report \
  --ipsw-meta /path/to/ipsw_meta.json \
  --ota-meta /path/to/ota_image_meta.json
```

Optional output path:

```sh
uv run symx artifacts v2 report \
  --ipsw-meta /path/to/ipsw_meta.json \
  --ota-meta /path/to/ota_image_meta.json \
  --output /tmp/symx-artifact-parity.json
```

## Files added/changed

```text
symx/artifacts/__init__.py
symx/artifacts/app/__init__.py
symx/artifacts/convert.py
symx/artifacts/ids.py
symx/artifacts/model.py
symx/artifacts/report.py
symx/__init__.py
tests/test_artifacts_v2.py
```

## Commands run

```sh
uv run pytest tests/test_artifacts_v2.py
uv run ruff check --fix
uv run ruff format
uv run pytest tests/test_artifacts_v2.py
uv run pyright
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest
uv run symx artifacts v2 report --help >/tmp/symx-artifacts-help.txt
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

The new tests verify:

- artifact UIDs are stable and domain-prefixed,
- IPSW conversion emits one normalized artifact per `IpswSource`,
- OTA conversion emits one normalized artifact per `OtaArtifact`,
- normalized artifact bundles JSON round-trip through Pydantic,
- artifact bundles reject missing/mismatched detail records,
- parity reporting matches current IPSW/OTA mirror/extract worklist eligibility for representative fixtures.

The full existing test suite still passes.

## Mismatches or surprises

No runtime mismatches yet because this iteration only used synthetic fixtures.

One design choice to watch during real metadata validation: v2 worklist parity currently models the existing workflow selection order closely enough for representative fixtures, but real metadata may expose tie-ordering cases. The first run against `gs://apple_symbols/` should inspect worklist mismatches carefully rather than treating them as automatically fatal.

## Changes to the production migration plan

This iteration establishes the local comparison baseline but does not yet shadow production-like storage writes.

Next migration pressure points:

- add a configurable GCS prefix store for normalized bundles,
- bootstrap from real legacy metadata into an experiment prefix,
- compare full real worklists and state counts,
- then introduce facade-level shadowing around actual storage updates.

## Next proposed iteration

Add a v2 GCS prefix store and a non-destructive bootstrap command:

```sh
uv run symx artifacts v2 bootstrap \
  --storage gs://apple_symbols/ \
  --prefix experiments/meta-v2/<run-id>
```

The command should:

- read legacy root metadata from the bucket,
- materialize normalized artifact/detail objects under the experiment prefix,
- use create-only writes by default,
- write a manifest and parity report,
- not touch root metadata, mirrors, or symbols.
