# Real Testbed Phase 3.0

This directory contains the first gate for the real-sensing testbed. It does not
change the existing Controller/Agent code. It only verifies that the server can
run Linux network namespaces and creates a small terminal/edge/cloud topology.

## Topology

`setup_topology.sh` creates four network namespaces:

- `h-term`: terminal node, `10.10.1.2/24`
- `h-edge`: edge node, `10.10.2.2/24`
- `h-cloud`: cloud node, `10.10.3.2/24`
- `h-router`: routes traffic between the three subnets

Each host namespace is connected to `h-router` by a veth pair. `iperf3` servers
are started in `h-edge` and `h-cloud`.

## Commands

Run the preflight first:

```bash
sudo bash testbed/preflight.sh
```

Create the topology:

```bash
sudo bash testbed/setup_topology.sh
```

Basic smoke checks:

```bash
sudo ip netns exec h-term ping -c 5 10.10.3.2
sudo ip netns exec h-term iperf3 -c 10.10.3.2 -t 3 -J
```

Inject a real network event on the router-to-cloud link:

```bash
sudo ip netns exec h-router tc qdisc replace dev rt-cloud0 root netem delay 80ms loss 5%
sudo ip netns exec h-term ping -c 20 10.10.3.2
```

Clear the injected event:

```bash
sudo ip netns exec h-router tc qdisc del dev rt-cloud0 root
```

Clean up:

```bash
sudo bash testbed/teardown.sh
```

## Expected Gate

Phase 3.0 is ready when:

- `preflight.sh` reports `READY`.
- `setup_topology.sh` creates all namespaces and passes its built-in ping/iperf3
  smoke checks.
- Adding `tc netem loss 5%` causes `ping` to observe packet loss.
- `teardown.sh` removes all namespaces and temporary logs.

If root or `CAP_NET_ADMIN` is missing, this phase cannot run. That is an
environment gate, not an application failure.
