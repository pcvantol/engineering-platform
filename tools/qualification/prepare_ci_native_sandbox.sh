#!/usr/bin/env bash
set -euo pipefail

# GitHub's disposable Ubuntu runner restricts user namespaces before Codex's
# filesystem/network sandbox can start (bwrap RTM_NEWADDR). Enable namespace
# creation only on that disposable host; the product's actual deny tests still
# have to pass. Never apply this setup to an installed operator host.
test "${GITHUB_ACTIONS:-}" = true
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
test "${RUNNER_OS:-}" = Linux
if [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
