# Iteration 007: symbol bundle identity and debugging materializations

Date: 2026-05-12

## What changed

The symbol-store scope was clarified further.

Changing future symsorter bundle IDs to use metadata `artifact_uid` is not categorically forbidden. It may be the cleanest future design. But existing symbol-store data must not be migrated or rekeyed.

Therefore, if future uploads ever switch to artifact-UID-based bundle IDs, Symx must intentionally support a mixed world:

- existing symbols reference legacy symsorter bundle IDs,
- newer symbols may reference artifact UID bundle IDs,
- metadata must record the actual `symbol_bundle_id` used for each artifact,
- tooling must not assume one universal derivation rule from artifact to symbol bundle ID.

The current converter still records the existing bundle IDs because it reflects current behavior.

## Symbolicator interface boundary

A new symbolicator-facing symbol-store interface remains out of scope.

However, separate Symx/debugging materializations are not ruled out for future design work. Examples that should remain possible in the model:

- list all symbols for a debug ID with offsets,
- resolve an offset for a debug ID,
- find the offset of `func_X` across selected fundamental frameworks for iOS 16,
- compare symbol presence across releases/frameworks.

Those would be read-optimized debugging/query features, not changes to symbolicator's existing contract.

## Files changed

```text
docs/meta-v2/000-working-thesis.md
docs/meta-v2/002-gcs-bucket-and-storage-invariants.md
docs/meta-v2/006-scope-and-source-provenance.md
docs/meta-v2/007-symbol-bundle-identity-and-debug-materializations.md
```

## Commands run

No application commands were run for this documentation-only clarification.

## GCS access

None.

No reads or writes were made against:

```text
gs://apple_symbols/
```

## Prefixes written

None.

## Validation results

Not applicable; documentation-only clarification.

## Mismatches or surprises

The previous documentation was too strong when it said artifact IDs must not replace symsorter bundle IDs. The corrected rule is narrower and more important:

- do not migrate/rekey existing symbol-store data,
- do not change symbolicator's interface,
- record the actual bundle ID used per artifact.

## Changes to the production migration plan

The v2 artifact model field `symbol_bundle_id` remains useful and should be interpreted as "the actual symbol bundle identity used", not necessarily "the legacy bundle identity".

Any future bundle-ID change must be evaluated as a forward-only storage behavior change with mixed historical identities.

## Next proposed iteration

Continue with the v2 GCS prefix store and bootstrap command. It should record current legacy bundle IDs from current metadata/conversion behavior and leave symbol storage untouched.
