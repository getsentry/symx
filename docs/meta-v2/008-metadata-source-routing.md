# Iteration 008: metadata source as routing, not just provenance

Date: 2026-05-12

## What changed

The source/provenance field was renamed conceptually and in code from `source_kind` / `ArtifactSourceKind` to:

```text
metadata_source
MetadataSource
```

The intent is now explicit: this field is primarily about allowing the same processing domain to be sourced from different upstream input models, not about provenance for its own sake.

Examples:

```text
kind = ota
metadata_source = apple_ota_feed
```

could later become or coexist with:

```text
kind = ota
metadata_source = appledb
```

Likewise:

```text
kind = sim
metadata_source = runner_sim_cache
```

could later coexist with:

```text
kind = sim
metadata_source = xcode_sim
```

## Why keep it

The field lets artifact records route to source-specific:

- detail schemas,
- input data paths,
- importer/update semantics,
- reconciliation rules,
- raw/source metadata retention.

Provenance is a useful side effect, but not the primary design goal.

## Files changed

```text
symx/artifacts/model.py
symx/artifacts/convert.py
tests/test_artifacts_v2.py
docs/meta-v2/000-working-thesis.md
docs/meta-v2/006-scope-and-source-provenance.md
docs/meta-v2/008-metadata-source-routing.md
```

## Commands run

```sh
uv run pytest tests/test_artifacts_v2.py
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest
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

The artifact-v2 tests now assert:

- IPSW converted records use `metadata_source = appledb`,
- OTA converted records use `metadata_source = apple_ota_feed`.

## Mismatches or surprises

None. This is a naming and intent clarification.

## Changes to the production migration plan

Future GCS layouts/detail paths should not assume that domain alone picks the detail schema. `kind=ota` may have OTA-feed details today and AppleDB details later. `kind=sim` may have runner-cache details today and xcode-simulator-package details later.

The canonical artifact record should therefore keep `kind` and `metadata_source` independent.

## Next proposed iteration

Continue with the v2 GCS prefix store and bootstrap command using `metadata_source` in artifact records.
