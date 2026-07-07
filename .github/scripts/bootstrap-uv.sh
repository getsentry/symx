#!/usr/bin/env bash
# Source this file from GitHub Actions to install pinned uv and update PATH.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bootstrap_env="$(python3 "$script_dir/gha_deps.py" bootstrap-uv --emit-shell)"
eval "$bootstrap_env"
