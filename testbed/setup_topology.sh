#!/usr/bin/env bash
set -Eeuo pipefail

NS_TERM=${NS_TERM:-h-term}
NS_EDGE=${NS_EDGE:-h-edge}
NS_CLOUD=${NS_CLOUD:-h-cloud}
NS_ROUTER=${NS_ROUTER:-h-router}

TERM_IP=${TERM_IP:-10.10.1.2}
EDGE_IP=${EDGE_IP:-10.10.2.2}
CLOUD_IP=${CLOUD_IP:-10.10.3.2}
TERM_GW=${TERM_GW:-10.10.1.1}
EDGE_GW=${EDGE_GW:-10.10.2.1}
CLOUD_GW=${CLOUD_GW:-10.10.3.1}
PREFIX=${PREFIX:-24}
IPERF_PORT=${IPERF_PORT:-5201}
STATE_DIR=${STATE_DIR:-/tmp/6g-agent-testbed}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "setup_topology.sh requires root because it creates netns/veth links and configures routing." >&2
    exit 1
  fi
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
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
  fi
}

cleanup_existing() {
  for ns in "$NS_TERM" "$NS_EDGE" "$NS_CLOUD" "$NS_ROUTER"; do
    delete_namespace "$ns"
  done
}

create_pair() {
  host_if=$1
  router_if=$2
  host_ns=$3
  host_name=$4
  router_name=$5

  ip link add "$host_if" type veth peer name "$router_if"
  ip link set "$host_if" netns "$host_ns"
  ip link set "$router_if" netns "$NS_ROUTER"
  ip -n "$host_ns" link set "$host_if" name "$host_name"
  ip -n "$NS_ROUTER" link set "$router_if" name "$router_name"
}

configure_host() {
  ns=$1
  dev=$2
  ip_addr=$3
  gateway=$4

  ip -n "$ns" link set lo up
  ip -n "$ns" addr add "${ip_addr}/${PREFIX}" dev "$dev"
  ip -n "$ns" link set "$dev" up
  ip -n "$ns" route add default via "$gateway"
}

configure_router_link() {
  dev=$1
  ip_addr=$2

  ip -n "$NS_ROUTER" addr add "${ip_addr}/${PREFIX}" dev "$dev"
  ip -n "$NS_ROUTER" link set "$dev" up
}

start_iperf_server() {
  ns=$1
  mkdir -p "$STATE_DIR"
  ip netns exec "$ns" iperf3 -s -D -p "$IPERF_PORT" --logfile "$STATE_DIR/iperf3-${ns}.log"
}

smoke_check() {
  echo "Running smoke checks..."
  ip netns exec "$NS_TERM" ping -c 1 -W 2 "$EDGE_IP" >/dev/null
  ip netns exec "$NS_TERM" ping -c 1 -W 2 "$CLOUD_IP" >/dev/null
  ip netns exec "$NS_CLOUD" ping -c 1 -W 2 "$TERM_IP" >/dev/null
  ip netns exec "$NS_TERM" iperf3 -c "$EDGE_IP" -p "$IPERF_PORT" -t 1 >/dev/null
  ip netns exec "$NS_TERM" iperf3 -c "$CLOUD_IP" -p "$IPERF_PORT" -t 1 >/dev/null
  ip netns exec "$NS_CLOUD" iperf3 -c "$TERM_IP" -p "$IPERF_PORT" -t 1 >/dev/null
  echo "Smoke checks passed: ping and iperf3 work across term/edge/cloud."
}

require_root
for cmd in ip iperf3 ping awk grep kill; do
  require_cmd "$cmd"
done

cleanup_existing
mkdir -p "$STATE_DIR"

ip netns add "$NS_TERM"
ip netns add "$NS_EDGE"
ip netns add "$NS_CLOUD"
ip netns add "$NS_ROUTER"

create_pair veth-term veth-rt-term "$NS_TERM" term0 rt-term0
create_pair veth-edge veth-rt-edge "$NS_EDGE" edge0 rt-edge0
create_pair veth-cloud veth-rt-cloud "$NS_CLOUD" cloud0 rt-cloud0

configure_host "$NS_TERM" term0 "$TERM_IP" "$TERM_GW"
configure_host "$NS_EDGE" edge0 "$EDGE_IP" "$EDGE_GW"
configure_host "$NS_CLOUD" cloud0 "$CLOUD_IP" "$CLOUD_GW"

ip -n "$NS_ROUTER" link set lo up
configure_router_link rt-term0 "$TERM_GW"
configure_router_link rt-edge0 "$EDGE_GW"
configure_router_link rt-cloud0 "$CLOUD_GW"
ip netns exec "$NS_ROUTER" sysctl -qw net.ipv4.ip_forward=1

start_iperf_server "$NS_TERM"
start_iperf_server "$NS_EDGE"
start_iperf_server "$NS_CLOUD"
smoke_check

cat <<EOF

Topology ready.

Namespaces:
  $NS_TERM   ${TERM_IP}/${PREFIX} via $TERM_GW
  $NS_EDGE   ${EDGE_IP}/${PREFIX} via $EDGE_GW
  $NS_CLOUD  ${CLOUD_IP}/${PREFIX} via $CLOUD_GW
  $NS_ROUTER routes between the three subnets

Useful commands:
  ip netns exec $NS_TERM ping -c 5 $CLOUD_IP
  ip netns exec $NS_TERM iperf3 -c $CLOUD_IP -t 3 -J
  ip netns exec $NS_TERM iperf3 -c $EDGE_IP -t 3 -J
  ip netns exec $NS_CLOUD iperf3 -c $TERM_IP -t 3 -J
  ip netns exec $NS_ROUTER tc qdisc replace dev rt-cloud0 root netem delay 80ms loss 5%
  ip netns exec $NS_ROUTER tc qdisc del dev rt-cloud0 root
EOF
