# Agent Instructions

## Package & Dependency Management

- Always use `uv` for running the application, dev tools, and managing dependencies.
- Use `uv run` to invoke any Python tool (`pytest`, `pyright`, `ruff`, etc.).
- Use `uv add` / `uv remove` for dependency changes.

## Clarification & Interaction

If something is unclear, ask questions early instead of inferring intent after a long working phase.

- Prefer a short set of concrete clarification questions over a speculative plan.
- Do this especially when requirements are ambiguous, there are multiple reasonable implementation paths, or a wording/detail could materially change the result.
- Do not disappear into a long investigation and only then present a guessed interpretation.

## Sources of Truth

For describing **current behavior**, prefer implementation and workflow files over assumptions or scratch notes.

- **Pipeline/runtime behavior:** `symx/ipsw/*`, `symx/ota/*`, `symx/sim/*`
- **CLI surface:** `symx/__init__.py`, `symx/*/app/__init__.py`
- **Admin behavior implemented today:** `symx/admin/*`
- **Shared processing-state vocabulary:** `symx/model.py`
- **Production deployment, schedules, runner types, and workflow entrypoints:** `.github/workflows/*.yml`

Untracked files are not automatically irrelevant. They may be part of the work in progress. The important distinction is:

- do not infer project-wide or production behavior from unrelated local scratch files or generated artifacts,
- but if an untracked file is in scope for the task, read and update it normally.

## Local Artifacts

This repo may contain local-only artifacts such as:

- scratch docs
- downloaded artifacts
- coverage output
- ad-hoc scripts
- investigation inputs

Guidance:

- do not treat unrelated local artifacts as supported project surfaces,
- do not clean them up unless explicitly asked,
- if a local file appears relevant but is not authoritative, verify against tracked code/workflows before relying on it for documentation.

## Architecture Invariants

Keep these distinctions straight:

- IPSW processing state is tracked per **`IpswSource`** inside an `IpswArtifact`.
- OTA processing state is tracked per **`OtaArtifact`**.
- GCS meta JSON files are the shared persisted state.
- GitHub Actions is the production execution/scheduling layer.
- Sentry is the observability layer.
- The admin surface implemented today is the code in `symx/admin/*`; do not infer extra capabilities from design ideas or scratch notes.
- The application should be able to run locally if a local GCS store is provided
- Target platforms are Linux and macOS but some features (extraction) can only run on macOS.

## Processing-State Guidance

- Do not assume every `ArtifactProcessingState` enum member is actively emitted by automation.
- Verify actual transitions by tracing assignments in runners/storage code.
- Distinguish between:
  - automated transitions,
  - migration/manual-only transitions,
  - reserved/unused states.
- When auditing or changing states, cross-check both:
  - code assignments in `symx/ipsw/*`, `symx/ota/*`, etc., and
  - the latest local admin snapshot if one is available under `~/.cache/symx/admin/snapshots/`.

## Documentation

Documentation that is out of date is useless.

- Every change should be cross-checked against inline comments and Markdown docs in the repo.
- Not every code change requires documentation updates, but every change that invalidates existing documentation must update the documentation in the same change.
- When documenting commands or production behavior, verify against current CLI help and workflow YAML instead of guessing from old prose.
- Prefer putting detailed architecture/operations material in `docs/*.md` and linking from `README.md`.

## Coverage & Testability

Changes should be covered by tests as much as is realistically possible.

Since symx is workflow- and side-effect-heavy, a lot of behavior does not make sense to cover end-to-end. Design should still keep the core behavior testable with small units and injectable side effects.

When changing workflow/state logic, update or add tests under `tests/` where practical.

For regression fixes, prefer test-first when feasible:

- ideally add or identify a test that reproduces the broken setup before attempting the fix,
- then make the code change,
- then verify the test passes together with the full relevant suite.

This should also guide development work in general: if, while investigating or implementing, you discover that the current code does not behave as expected, try to capture that setup in a test before changing the code.

This is especially important when fixing subtle state-machine, storage, concurrency, or recovery-path behavior.

## Storage & Concurrency

Storage is holy.

- Overwrites are usually not something we do.
- Treat symbol storage as additive/create-only unless the task explicitly requires something else.
- Be extremely careful with metadata writes. Never introduce changes that could leave metadata in a corrupt or partially-written state because concurrency was not considered.
- When changing storage/meta-data behavior, think through concurrent workflows explicitly and preserve the existing safety properties (for example generation-matched writes / optimistic concurrency where applicable).
- If a proposed change weakens concurrency safety or makes destructive overwrites more likely, call that out clearly before proceeding.

## Safety

Be explicit when suggesting commands that can mutate shared state.

- Any command using `--storage gs://...` may read from and write to shared storage.
- Prefer disposable buckets for development or reproduction.
- If a command appears to target production-like infrastructure, call that out clearly.

## Verification

For code changes, run the full check suite across **all files** (not just changed ones):

```sh
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest
```

Markdown-only changes do not require the full verification suite.
