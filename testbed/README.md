# Linux network-namespace testbed

The testbed builds terminal, edge, cloud, and router namespaces for real ping, iperf3, `ss`, and `tc netem` measurements. It requires Linux with root or `CAP_NET_ADMIN`.

```bash
sudo bash testbed/preflight.sh
sudo bash testbed/setup_topology.sh
sudo ip netns exec h-term ping -c 5 10.10.3.2
sudo bash testbed/teardown.sh
```

Use `python -m experiments.real_probe`, `python -m experiments.real_run`, `python -m experiments.e2e_build`, or `python -m experiments.e2e_recovery` after setup. The canonical measured experiment is launched by `scripts/run_wcnc_final_v3_remote.sh` and is documented in `docs/wcnc_final_v3.md`.

The topology scripts are intentionally not executed in ordinary GitHub-hosted CI. Their shell syntax is checked there; full behavior must be verified on a dedicated privileged Linux runner.
