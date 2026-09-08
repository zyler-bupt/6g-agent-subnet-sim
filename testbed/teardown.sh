#!/usr/bin/env bash
set -Eeuo pipefail

NS_TERM=${NS_TERM:-h-term}
NS_EDGE=${NS_EDGE:-h-edge}
NS_CLOUD=${NS_CLOUD:-h-cloud}
NS_ROUTER=${NS_ROUTER:-h-router}
STATE_DIR=${STATE_DIR:-/tmp/6g-agent-testbed}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "teardown.sh requires root because it deletes network namespaces." >&2
    exit 1
  fi
}

namespace_exists() {
  ip netns list | awk '{print $1}' | grep -qx "$1"
}

kill_namespace_processes() {
  ns=$1
  if namespace_exists "$ns"; then
    pids=$(ip netns pids "$ns" || true)
    if [ -n "$pids" ]; then
      # shellcheck disable=SC2086
      kill $pids >/dev/null 2>&1 || true
      sleep 0.2
      # shellcheck disable=SC2086
      kill -9 $pids >/dev/null 2>&1 || true
    fi
  fi
}

delete_namespace() {
  ns=$1
  if namespace_exists "$ns"; then
    kill_namespace_processes "$ns"
    ip netns del "$ns"
    echo "deleted namespace: $ns"
  fi
}

require_root

for ns in "$NS_TERM" "$NS_EDGE" "$NS_CLOUD" "$NS_ROUTER"; do
  delete_namespace "$ns"
done

rm -rf "$STATE_DIR"
echo "testbed teardown complete"
