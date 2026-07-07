# GitHub Actions dependency pins

GitHub Actions is part of the production execution path for Symx, so GitHub Actions bootstrap dependencies are pinned and verified instead of piping downloaded shell scripts into a shell or using floating `latest` URLs.

The entrypoint for resolving, installing, verifying, and bumping GitHub Actions bootstrap dependencies is:

```bash
python3 .github/scripts/gha_deps.py --help
```

The pin manifest is [`.github/gha-deps.json`](../.github/gha-deps.json). The script is stdlib-only so workflows can run it before `uv sync`.

## Current pin locations

| Dependency                  | Source of truth                                                                             | Runtime use                                                                                                              |
|-----------------------------|---------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| Project Python dependencies | [`pyproject.toml`](../pyproject.toml) and [`uv.lock`](../uv.lock)                           | `uv sync`                                                                                                                |
| `uv` bootstrap package      | `.github/gha-deps.json` `uv` section                                                        | [`.github/scripts/bootstrap-uv.sh`](../.github/scripts/bootstrap-uv.sh) calls `gha_deps.py bootstrap-uv`                 |
| Google Cloud SDK            | `.github/gha-deps.json` `gcloud` section plus exact workflow `version:` pins                | `google-github-actions/setup-gcloud`                                                                                     |
| `blacktop/ipsw` CLI         | `.github/gha-deps.json` `ipsw` section                                                      | [`.github/actions/install-ipsw/action.yml`](../.github/actions/install-ipsw/action.yml) calls `gha_deps.py install-ipsw` |
| `symsorter`                 | `.github/gha-deps.json` `symsorter` section                                                 | `gha_deps.py install-symsorter`                                                                                          |
| Apple root certificate      | `.github/gha-deps.json` `apple_root` section                                                | `gha_deps.py install-apple-root`                                                                                         |
| GitHub Actions              | [`.github/workflows/`](../.github/workflows/) and [`.github/actions/`](../.github/actions/) | Third-party actions are pinned by commit SHA with the upstream tag in a comment.                                         |

All direct GitHub Actions file downloads should go through `gha_deps.py`, which requires HTTPS and verifies SHA-256 before using downloaded content.

## Runtime commands used by workflows

```bash
source .github/scripts/bootstrap-uv.sh
python3 .github/scripts/gha_deps.py install-ipsw --platform macos --install-dir /usr/local/bin --sudo
python3 .github/scripts/gha_deps.py install-ipsw --platform linux --install-dir /usr/local/bin --sudo
python3 .github/scripts/gha_deps.py install-symsorter --output symsorter
python3 .github/scripts/gha_deps.py install-apple-root
```

## Bumping project Python dependencies

Use the standard project dependency workflow:

```bash
uv add <package>==<version>
uv add --dev <package>==<version>
uv remove <package>
uv remove --dev <package>
uv lock --upgrade-package <package>
```

Then run `uv sync` and the full verification suite.

## Bumping `uv`

Preview the hashes for a target version:

```bash
python3 .github/scripts/gha_deps.py uv-hashes --version 0.11.26
```

Preview and then apply the manifest update:

```bash
python3 .github/scripts/gha_deps.py bump-uv --version 0.11.26
python3 .github/scripts/gha_deps.py bump-uv --version 0.11.26 --write
```

Validate the rendered hash-checked requirements for the current platform:

```bash
python3 .github/scripts/gha_deps.py verify --network
```

## Bumping Google Cloud SDK

Check the current rapid channel version:

```bash
python3 .github/scripts/gha_deps.py gcloud-latest
```

Preview and then apply the manifest/workflow update:

```bash
python3 .github/scripts/gha_deps.py bump-gcloud --latest
python3 .github/scripts/gha_deps.py bump-gcloud --latest --write
```

Use `--version <version>` instead of `--latest` to pin a specific version.

## Bumping `ipsw`

Preview the checksums for the two workflow archives:

```bash
python3 .github/scripts/gha_deps.py ipsw-checksums --version 3.1.685
```

Preview and then apply the manifest update:

```bash
python3 .github/scripts/gha_deps.py bump-ipsw --version 3.1.685
python3 .github/scripts/gha_deps.py bump-ipsw --version 3.1.685 --write
```

Workflows should not override `ipsw` versions directly; update `.github/gha-deps.json` through `bump-ipsw` instead.

## Bumping `symsorter`

`symsorter` is released as part of `getsentry/symbolicator`.

Preview the hash for a target release:

```bash
python3 .github/scripts/gha_deps.py symsorter-sha --version 26.6.0
```

Preview and then apply the manifest update:

```bash
python3 .github/scripts/gha_deps.py bump-symsorter --version 26.6.0
python3 .github/scripts/gha_deps.py bump-symsorter --version 26.6.0 --write
```

## Bumping pinned GitHub Actions

For each `uses: owner/repo@<sha> # <tag>` line:

1. Pick the new upstream tag.
2. Resolve the tag to a commit SHA:

   ```bash
   python3 .github/scripts/gha_deps.py resolve-action --repo actions/checkout --tag v6.0.2
   ```

3. Replace the SHA in the workflow/action file and update the trailing tag comment.

The resolver uses the peeled commit for annotated tags.

## Apple root certificate

This should rarely change. If it does, inspect the current remote certificate:

```bash
python3 .github/scripts/gha_deps.py apple-root-sha --details
```

Preview and then apply the manifest update:

```bash
python3 .github/scripts/gha_deps.py bump-apple-root
python3 .github/scripts/gha_deps.py bump-apple-root --write
```

## Verification after GitHub Actions dependency changes

Run the GitHub Actions dependency policy check:

```bash
python3 .github/scripts/gha_deps.py verify
```

Add network verification for remotely resolved hashes. This downloads the current-platform `uv` wheel, the Apple root certificate, and `symsorter`:

```bash
python3 .github/scripts/gha_deps.py verify --network
```

To also re-download the larger pinned `ipsw` archives:

```bash
python3 .github/scripts/gha_deps.py verify --network --large-downloads
```

For code changes beyond workflow/docs bootstrap wiring, also run the full project suite from the repository root:

```bash
uv run ruff check --fix
uv run ruff format
uv run pyright
uv run pytest
```
