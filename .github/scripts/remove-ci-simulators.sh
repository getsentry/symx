#!/usr/bin/env bash
# Destructive preparation for disposable OTA/IPSW CI runners ONLY.
# Never call this from local extraction or simulator-symbol collection.
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_OS:-}" != macOS || -z "${GITHUB_WORKSPACE:-}" ]]; then
    echo 'Simulator cleanup is allowed only on a disposable macOS Actions extraction runner' >&2
    exit 2
fi

report_final_disk_space() {
    local status=$?
    trap - EXIT
    echo "## Disk space after simulator cleanup (exit $status)"
    df -h "$GITHUB_WORKSPACE" "${TMPDIR:-/tmp}" || true
    exit "$status"
}
trap report_final_disk_space EXIT

echo '## Disk space before simulator cleanup'
df -h "$GITHUB_WORKSPACE" "${TMPDIR:-/tmp}"
echo '## Simulator runtimes before cleanup'
xcrun simctl runtime list

# Use CoreSimulator's lifecycle APIs: do not recursively delete mounted images,
# Xcode, SDKs, platform directories, or arbitrary /Library or /System paths.
xcrun simctl shutdown all
xcrun simctl delete all
xcrun simctl runtime dyld_shared_cache remove --all
# Do not pass --keep-asset: the associated MobileAssets also consume disk.
# This operation shuts down users of runtime images and unmounts them first.
xcrun simctl runtime delete all

echo '## Simulator runtimes after cleanup'
xcrun simctl runtime list
