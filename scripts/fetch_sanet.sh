#!/usr/bin/env bash
set -euo pipefail

readonly repo_url="https://github.com/WirelessAIatHUST/SANet.git"
readonly pinned_commit="60d9b3c1db02aa2018e67b0020a0feb57d9e3d73"
readonly target="${1:-local/baselines/SANet-upstream}"

if [[ -e "$target" ]]; then
  echo "target already exists: $target" >&2
  exit 2
fi

mkdir -p "$(dirname "$target")"
git clone --filter=blob:none --no-checkout "$repo_url" "$target"
git -C "$target" checkout --detach "$pinned_commit"

actual_commit="$(git -C "$target" rev-parse HEAD)"
if [[ "$actual_commit" != "$pinned_commit" ]]; then
  echo "SANet commit verification failed: expected $pinned_commit, got $actual_commit" >&2
  exit 1
fi

echo "SANet checked out at verified commit $actual_commit in $target"
