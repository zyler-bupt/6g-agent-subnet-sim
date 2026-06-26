#!/usr/bin/env bash
set -u

FAILURES=0
WARNINGS=0

pass() {
  printf '[PASS] %s\n' "$1"
}

fail() {
  printf '[FAIL] %s\n' "$1"
  FAILURES=$((FAILURES + 1))
}

warn() {
  printf '[WARN] %s\n' "$1"
  WARNINGS=$((WARNINGS + 1))
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

printf '6G agent real testbed preflight\n'
printf '================================\n'

if [ "$(uname -s)" = "Linux" ]; then
  pass "Linux kernel detected: $(uname -r)"
else
  fail "This testbed requires Linux; detected $(uname -s)"
fi

if [ "$(id -u)" -eq 0 ]; then
  pass "running as root"
else
  fail "not running as root; netns/veth/tc setup requires root or equivalent capabilities"
fi

for cmd in ip tc ss ping iperf3 awk grep sed sysctl; do
  if have_cmd "$cmd"; then
    pass "found $cmd: $(command -v "$cmd")"
  else
    fail "missing required command: $cmd"
  fi
done

if have_cmd ip; then
  if ip netns list >/dev/null 2>&1; then
    pass "ip netns can query namespaces"
  else
    fail "ip netns query failed; missing CAP_NET_ADMIN/CAP_SYS_ADMIN or netlink access"
  fi
fi

if have_cmd tc; then
  if tc qdisc show >/dev/null 2>&1; then
    pass "tc can query qdisc state"
  else
    fail "tc qdisc query failed; missing CAP_NET_ADMIN or rtnetlink access"
  fi
fi

if [ -r /proc/sys/net/ipv4/tcp_congestion_control ]; then
  current_cc=$(cat /proc/sys/net/ipv4/tcp_congestion_control)
  pass "current TCP congestion control: ${current_cc}"
else
  warn "cannot read current TCP congestion control"
fi

if [ -r /proc/sys/net/ipv4/tcp_available_congestion_control ]; then
  available_cc=$(cat /proc/sys/net/ipv4/tcp_available_congestion_control)
  pass "available TCP congestion controls: ${available_cc}"
  case " ${available_cc} " in
    *" bbr "*) pass "BBR is available" ;;
    *) warn "BBR is not listed; tAgent can still use cubic, but cubic<->bbr switching will be unavailable" ;;
  esac
else
  warn "tcp_available_congestion_control is not exposed on this system; BBR availability cannot be confirmed"
fi

for module in sch_netem sch_htb; do
  if grep -qw "$module" /proc/modules 2>/dev/null; then
    pass "kernel module loaded: $module"
  elif have_cmd modprobe && modprobe -n "$module" >/dev/null 2>&1; then
    pass "kernel module can be loaded on demand: $module"
  else
    warn "could not confirm kernel module support: $module"
  fi
done

if grep -qw tcp_bbr /proc/modules 2>/dev/null; then
  pass "kernel module loaded: tcp_bbr"
elif have_cmd modprobe && modprobe -n tcp_bbr >/dev/null 2>&1; then
  pass "tcp_bbr can be loaded on demand"
else
  warn "could not confirm tcp_bbr support"
fi

printf '\nSummary\n'
printf '%s\n' '-------'
printf 'failures=%s warnings=%s\n' "$FAILURES" "$WARNINGS"

if [ "$FAILURES" -eq 0 ]; then
  printf 'READY: Phase 3.0 topology setup can run on this host.\n'
  exit 0
fi

printf 'NOT READY: fix the failed checks before running setup_topology.sh.\n'
exit 1
