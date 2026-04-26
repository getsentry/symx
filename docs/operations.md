# Operations, deployment, and debugging

This document is the practical companion to [architecture.md](architecture.md). It covers how to run Symx locally, how the current deployment is wired, and how to inspect and debug production behavior.

## 1. Local setup

## 1.1 Base requirements

- Python **3.14+**
- [`uv`](https://docs.astral.sh/uv/)
- this repository checked out locally

Install Python dependencies:

```bash
uv sync --dev
```

The project exposes a `symx` console script, so after `uv sync` the usual entrypoint is:

```bash
uv run symx --help
```

## 1.2 Tooling requirements by task

### Admin / workflow inspection only

Needed for:

- `uv run symx admin`
- `uv run symx admin sync`
- direct GitHub Actions inspection with `gh`

Requirements:

- `gh` CLI installed
- `gh auth login` completed with access to this repository

These commands do **not** need direct GCP credentials locally, because the actual metadata fetch happens inside a GitHub Actions workflow.

### GCS-backed Symx runs

Needed for:

- `uv run symx ipsw meta-sync ...`
- `uv run symx ipsw mirror ...`
- `uv run symx ipsw extract ...`
- `uv run symx ota mirror ...`
- `uv run symx ota extract ...`
- `uv run symx sim extract ...`

Requirements:

- Google Cloud credentials available via ADC (`gcloud auth application-default login`) or `GOOGLE_APPLICATION_CREDENTIALS`
- a storage URI accepted by Symx, for example:
  - `gs://my-bucket`
  - `gs://my-project@my-bucket`

> [!CAUTION]
> These commands read and write the configured bucket directly. Use a non-production bucket unless you intentionally want to operate on production data.

### Extraction commands

Needed for:

- `uv run symx ipsw extract-file ...`
- `uv run symx ota extract-file ...`
- `uv run symx ipsw extract ...`
- `uv run symx ota extract ...`
- `uv run symx sim extract ...`

Requirements:

- `ipsw` installed and on `PATH`
- executable `./symsorter` at the repository root

In practice, the extraction paths are run on **macOS** in production because they rely on the `ipsw` toolchain, DMG mount flows, and the platform-specific `symsorter` binary.

### Simulator extraction

Needed for:

- `uv run symx sim extract ...`

Additional requirement:

- a machine with Xcode simulator dyld caches at one of:
  - `/Library/Developer/CoreSimulator/Caches/dyld`
  - `~/Library/Developer/CoreSimulator/Caches/dyld`

## 1.3 Quick sanity checks

Helpful local checks before debugging the actual workflows:

```bash
uv run symx --help
ipsw version
./symsorter --version
gh auth status
```

## 2. Running Symx locally

## 2.1 Local, file-based reproduction

These commands are the fastest route when you already have the artifact locally and want to reproduce extraction behavior.

### IPSW

```bash
uv run symx ipsw extract-file /path/to/file.ipsw -p iOS -o /tmp/symx-ipsw
```

What it does:

- validates `ipsw` and `./symsorter`,
- runs the IPSW extraction pipeline locally,
- prints the output directory containing the symsorter result.

### OTA

```bash
uv run symx ota extract-file /path/to/file.zip -p ios -V 18.2 -b 22C152 -o /tmp/symx-ota
```

What it does:

- validates `ipsw` and `./symsorter`,
- runs the OTA extraction pipeline locally,
- prints the output directories containing the symsorter result.

## 2.2 GCS-backed runs from your machine

These are the same commands the production workflows ultimately run, just executed from your laptop or workstation.

### IPSW

```bash
uv run symx ipsw meta-sync -s gs://my-project@my-bucket
uv run symx ipsw mirror -s gs://my-project@my-bucket -t 60
uv run symx ipsw extract -s gs://my-project@my-bucket -t 60
uv run symx ipsw migrate -s gs://my-project@my-bucket
```

### OTA

```bash
uv run symx ota mirror -s gs://my-project@my-bucket
uv run symx ota extract -s gs://my-project@my-bucket -t 60
uv run symx ota migrate-storage -s gs://my-project@my-bucket
```

### Simulator

```bash
uv run symx sim extract -s gs://my-project@my-bucket
```

Use these primarily for:

- development against a disposable bucket,
- one-off reproduction of a production workflow with explicit intent,
- validating changes before wiring them into GitHub Actions.

## 2.3 Admin workflow and TUI

### Sync the local snapshot cache

```bash
uv run symx admin sync
```

What happens:

1. Symx uses `gh workflow run symx-admin-meta-sync.yml`.
2. GitHub Actions fetches the remote IPSW and OTA metadata plus their GCS generations.
3. Symx downloads the workflow artifact locally.
4. Symx builds a SQLite snapshot under `~/.cache/symx/admin/snapshots/<snapshot_id>/snapshot.db`.
5. `manifest.json` is updated to point to the active snapshot.

### Start the TUI

```bash
uv run symx admin
```

Current TUI capabilities:

- auto-sync on startup if no cached snapshot exists or the cached snapshot is older than 24h,
- separate IPSW and OTA failure tables,
- filter by processing state,
- show OTA GitHub run metadata when available,
- queue artifact downloads into `~/.cache/symx/admin/downloads/`,
- build curated rerun batches for eligible rows and apply them through GitHub Actions.

Useful variants:

```bash
uv run symx admin --failure-state symbol_extraction_failed
uv run symx admin --failure-state mirroring_failed --failure-state mirror_corrupt
uv run symx admin --cache-dir /tmp/symx-admin
```

### Query the SQLite snapshot directly

If you want a raw view instead of the TUI, inspect the local DB:

```bash
sqlite3 ~/.cache/symx/admin/snapshots/<snapshot_id>/snapshot.db '.tables'
```

Available tables:

- `snapshot_info`
- `ipsw_artifacts`
- `ipsw_sources`
- `ota_artifacts`

## 2.4 GitHub Actions inspection with `gh`

Use the GitHub CLI directly when you want to inspect workflow runs outside the admin TUI.

List known workflows:

```bash
gh workflow list
```

List recent runs for a workflow:

```bash
gh run list --workflow "Extract OTA symbols" --limit 20
gh run list --workflow "Mirror IPSW artifacts" --status failure --limit 20
gh run list --workflow symx-ota-extract.yml --limit 20
```

View or grep a specific run log:

```bash
gh run view 123456789 --log
gh run view 123456789 --log | grep -n "error" -C 2
```

## 3. Current deployment scenario

## 3.1 Source of truth

The current production system is rooted in three places:

- **GitHub Actions** for scheduling and execution,
- **GCS** for persisted state and artifacts,
- **Sentry** for observability.

There is no always-on service and no separate relational production database behind the workflows today.

## 3.2 Workflow entrypoints

| Workflow file                                    | Purpose                              | Command run inside workflow                                                           |
|--------------------------------------------------|--------------------------------------|---------------------------------------------------------------------------------------|
| `.github/workflows/symx-ipsw-meta-sync.yml`      | refresh IPSW metadata                | `symx ipsw meta-sync -s $SYMX_STORE`                                                  |
| `.github/workflows/symx-ipsw-mirror.yml`         | mirror IPSWs                         | `symx ipsw mirror -t 315 -s $SYMX_STORE`                                              |
| `.github/workflows/symx-ipsw-extract.yml`        | extract IPSW symbols                 | `symx ipsw extract -t 315 -s $SYMX_STORE`                                             |
| `.github/workflows/symx-ota-mirror.yml`          | refresh OTA metadata and mirror OTAs | `symx ota mirror -s $SYMX_STORE`                                                      |
| `.github/workflows/symx-ota-extract.yml`         | extract OTA symbols                  | `symx ota extract -t 330 -s $SYMX_STORE`                                              |
| `.github/workflows/symx-ota-migrate-storage.yml` | reset failed OTA extractions         | `symx ota migrate-storage -s $SYMX_STORE`                                             |
| `.github/workflows/symx-ipsw-migrate.yml`        | manual IPSW migration entrypoint     | `symx ipsw migrate -s $SYMX_STORE`                                                    |
| `.github/workflows/symx-simulator-extract.yml`   | upload simulator symbols             | `symx sim extract -s $SYMX_STORE`                                                     |
| `.github/workflows/symx-admin-meta-sync.yml`     | build admin snapshot inputs          | shell script using `gcloud storage`                                                   |
| `.github/workflows/symx-admin-apply.yml`         | apply curated admin rerun batches    | `symx admin apply-batch --storage "$SYMX_STORE" --request-json ... --result-path ...` |

For the workflows that use the reusable Symx runner wrappers, the command is not shell-interpolated directly. Those reusable workflows pass `SYMX_RUN` into `scripts/run_symx_gha.py`, which safely tokenizes it and executes `python -m symx ...`. The admin meta-sync, admin apply, and simulator workflows are custom wrappers around the same CLI.

## 3.3 Reusable workflow layers

### Ubuntu reusable workflow

File: [`.github/workflows/symx-runner-ubuntu.yml`](../.github/workflows/symx-runner-ubuntu.yml)

It does the following:

- checks out the repository,
- authenticates to GCP via GitHub OIDC,
- installs the Cloud SDK,
- installs `uv` and Python dependencies,
- installs the `ipsw` CLI,
- installs the Apple root certificate,
- removes large preinstalled toolchains to free disk,
- runs `scripts/run_symx_gha.py`.

This is used for meta sync and mirroring stages.

### macOS reusable workflow

File: [`.github/workflows/symx-runner-macos.yml`](../.github/workflows/symx-runner-macos.yml)

It does the following:

- checks out the repository,
- authenticates to GCP via GitHub OIDC,
- installs the Cloud SDK,
- installs `uv` and Python dependencies,
- installs the `ipsw` CLI,
- downloads a platform-appropriate `symsorter` binary,
- runs `scripts/run_symx_gha.py`.

This is used for extraction stages.

## 3.4 Variables, secrets, and credentials

From the workflow files, the important pieces are:

- repo variable `SYMX_STORE` – bucket URI used by Symx
- secret `SENTRY_DSN` – enables Sentry instrumentation
- implicit `GITHUB_TOKEN` – available inside GitHub Actions
- GCP auth via GitHub OIDC + Workload Identity, configured in the workflow files

For the authoritative details, read the workflow YAML files themselves.

## 4. Monitoring and debugging

<a id="github-actions"></a>

## 4.1 GitHub Actions

GitHub Actions is the first place to answer:

- did the workflow trigger,
- which runner did it use,
- did it fail in bootstrap or in Symx itself,
- how long did it run,
- which run ID should I search for elsewhere.

### Recommended commands

```bash
gh workflow list
gh run list --workflow "Sync IPSW meta-db" --limit 10
gh run list --workflow "Extract IPSW symbols" --status failure --limit 20
gh run view <run-id> --log | grep -n "Traceback" -C 3
```

### What to look for

- bootstrap failures before Symx starts:
  - `uv sync`
  - `ipsw` install/download
  - `symsorter` setup
  - GCP auth / Cloud SDK setup
- resource issues:
  - disk pressure,
  - timeouts,
  - runner image problems
- Symx failures after the `Running: ... python -m symx ...` line from `run_symx_gha.py`

<a id="admin-tui-and-local-snapshots"></a>

## 4.2 Admin TUI and local snapshots

The admin surface is the easiest way to inspect current failure rows without manually pulling JSON from GCS.

### Typical workflow

1. `uv run symx admin sync`
2. `uv run symx admin`
3. inspect IPSW or OTA failure rows
4. press `d` in the TUI to queue a download for the selected row, or `e` / `m` to build a curated rerun batch
5. reproduce with `extract-file`, or apply the batch with `a` if a rerun is the right next step

### Default failure states shown by the TUI

The default filter is:

- `mirroring_failed`
- `mirror_corrupt`
- `symbol_extraction_failed`
- `indexed_invalid`

The shared vocabulary still includes the manual/operator-only `ignored` state, but the current automation does not emit it and the admin defaults do not include it. Expected terminal skip states such as `delta_ota`, `recovery_ota`, and `unsupported_ota_payload` are also outside the default failure view.

### Useful local paths

- snapshot manifest: `~/.cache/symx/admin/manifest.json`
- active snapshots: `~/.cache/symx/admin/snapshots/`
- downloaded artifacts: `~/.cache/symx/admin/downloads/`
- GitHub run cache used by the TUI: `~/.cache/symx/admin/github_runs.json`

<a id="sentry"></a>

## 4.3 Sentry

Sentry is the live, per-run observability surface.

### Transaction names / ops worth knowing

Top-level transactions include:

- `ipsw.meta_sync`
- `ipsw.mirror`
- `ipsw.extract`
- `ipsw.extract_file`
- `ota.meta_sync`
- `ota.mirror`
- `ota.extract`
- `ota.extract_file`
- `sim.extract`

Common child span ops include:

- `http.download`
- `gcs.download`
- `gcs.upload`
- `gcs.upload_symbols`
- `subprocess.dyld_split`
- `subprocess.symsort`
- `subprocess.ipsw_extract`
- `subprocess.ipsw_ota_extract`
- `subprocess.hdiutil_mount`
- `ota.extract.payload_probe`

### High-value tags

Global:

- `github.run.id`

IPSW-specific:

- `ipsw.artifact.key`
- `ipsw.artifact.platform`
- `ipsw.artifact.version`
- `ipsw.artifact.build`
- `ipsw.artifact.source`
- `ipsw.version`
- `symsorter.version`

OTA-specific:

- `artifact.key`
- `artifact.platform`
- `artifact.version`
- `artifact.build`
- `artifact.url`

### Metrics worth watching

Representative counters/distributions/gauges emitted by the current code:

- `ipsw.mirror.succeeded`
- `ipsw.mirror.failed`
- `ipsw.extract.succeeded`
- `ipsw.extract.failed`
- `ipsw.extract.mirror_corrupt`
- `ota.mirror.succeeded`
- `ota.mirror.failed`
- `ota.extract.succeeded`
- `ota.extract.failed`
- `ota.extract.skipped_delta`
- `ota.extract.skipped_recovery`
- `ota.extract.skipped_unsupported_payload`
- `download.size_bytes`
- `download.duration_seconds`
- `gcs.upload.size_bytes`
- `gcs.download.size_bytes`
- `symbols.uploaded_new`
- `symbols.duplicates`
- `symbols.total_files`
- `disk.free_bytes`
- `disk.used_percent`

### Practical Sentry debugging loop

If you have a failed GitHub run:

1. get the run ID,
2. open the Sentry transaction tagged with `github.run.id=<run-id>`,
3. inspect the failing child span (`http.download`, `gcs.download`, `subprocess.*`, etc.),
4. use the artifact tags to identify the specific IPSW source or OTA artifact,
5. reproduce locally if needed.

## 4.4 Bucket-level escape hatches

When you cannot or do not want to authenticate directly to production GCS from your machine, there are helper workflows in `.github/workflows/`:

- `bucket-ls.yml`
- `bucket-objects-list.yml`
- `bucket-query-meta.yml`
- `bucket-cp.yml`
- `bucket-rm.yml`

These are useful for ad-hoc inspection of the bucket through GitHub Actions.

## 4.5 Common failure modes and first checks

### `mirroring_failed` (IPSW)

Likely causes:

- Apple CDN download failure
- SHA-1/size verification mismatch
- repeated transient download errors

First checks:

- failing GitHub run log
- Sentry `http.download` span
- source URL and expected hash in admin snapshot

### `indexed_invalid` (OTA)

Likely causes:

- OTA download failure from Apple
- SHA-1 verification mismatch
- mirror/upload failure

First checks:

- failing GitHub run log
- Sentry OTA mirror transaction
- OTA URL/hash from the admin snapshot

### `mirror_corrupt` (IPSW)

Likely causes:

- mirror object disappeared from GCS
- mirror object could be downloaded but failed verification

First checks:

- inspect `mirror_path`
- verify object existence via bucket workflow or GCS access
- check whether the source should be reset and mirrored again

### OTA reset from `mirrored` back to `indexed`

Likely cause:

- `load_ota()` could not retrieve the mirrored zip from GCS during extraction

What Symx does:

- clears `download_path`
- resets state to `indexed`
- relies on a later mirror run to repopulate the mirror

### `symbol_extraction_failed`

Likely causes:

- `ipsw` extraction failure
- `dyld_split` failure
- `symsorter` failure
- unexpected artifact layout differences

Best next step:

- download the artifact locally via the admin TUI,
- run `extract-file` locally,
- compare local stdout/stderr and Sentry spans with the workflow run.

### `delta_ota` / `recovery_ota`

These are usually **expected skip states**, not operator emergencies.

They mean the OTA does not contain a full DSC that Symx can process.

### `unsupported_ota_payload`

This is also usually a terminal skip state rather than an operator emergency.

It means the OTA appears to reference a full DSC, but the current payloadv2 / Apple Archive tooling cannot materialize it. In other words, this is different from delta or recovery OTAs: the DSC seems to exist conceptually, but current automation cannot extract it.

## 5. Current recovery mechanisms

There is no free-form general-purpose state editor in production today.

The implemented recovery surface is intentionally narrower: curated admin rerun batches plus the workflow-driven migration tools below.

### Admin curated reruns

Workflow: [`symx-admin-apply.yml`](../.github/workflows/symx-admin-apply.yml)

Current behavior:

- applies a curated batch reviewed from the local admin snapshot,
- validates the reviewed snapshot generation against the current remote generation,
- updates eligible rows back to `indexed` or `mirrored` depending on the selected action,
- dispatches the corresponding worker workflow when possible.

This is intentionally narrower than arbitrary state editing.

### IPSW

Workflow: [`symx-ipsw-migrate.yml`](../.github/workflows/symx-ipsw-migrate.yml)

Current behavior:

- runs `symx ipsw migrate`
- resets a hard-coded list of IPSW sources in `symbol_extraction_failed` back to `mirrored`

This is intentionally narrow and code-driven.

### OTA

Workflow: [`symx-ota-migrate-storage.yml`](../.github/workflows/symx-ota-migrate-storage.yml)

Current behavior:

- runs `symx ota migrate-storage`
- resets all OTA artifacts in `symbol_extraction_failed` back to `mirrored`

This is broader than the IPSW path, but still workflow-driven rather than interactive.

## 6. Before shipping code changes

The repo convention is to use `uv` for everything and to run the full check suite:

```bash
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest
```
