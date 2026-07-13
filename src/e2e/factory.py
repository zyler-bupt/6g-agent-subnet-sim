from __future__ import annotations

from src.metrics.netns import NetnsMetricProvider, NetnsTarget


def build_rescue_netns_provider(
    *,
    term_namespace: str = "h-term",
    edge_namespace: str = "h-edge",
    cloud_namespace: str = "h-cloud",
    term_ip: str = "10.10.1.2",
    edge_ip: str = "10.10.2.2",
    cloud_ip: str = "10.10.3.2",
    iperf_port: int = 5201,
    ping_count: int = 3,
    ping_interval_s: float = 0.2,
    iperf_seconds: int = 1,
    command_timeout_s: float = 8.0,
    sudo: bool = False,
) -> NetnsMetricProvider:
    def target(namespace: str, target_ip: str, label: str) -> NetnsTarget:
        return NetnsTarget(
            namespace=namespace,
            target_ip=target_ip,
            iperf_port=iperf_port,
            ping_count=ping_count,
            ping_interval_s=ping_interval_s,
            iperf_seconds=iperf_seconds,
            command_timeout_s=command_timeout_s,
            sudo=sudo,
            label=label,
        )

    targets = {
        "nagent-gw-ue": target(term_namespace, edge_ip, "terminal -> edge video stream"),
        "nagent-gw-mec": target(edge_namespace, cloud_ip, "edge -> cloud recognition result"),
        "nagent-gw-cloud": target(cloud_namespace, term_ip, "cloud -> terminal dispatch feedback"),
    }
    return NetnsMetricProvider(
        targets=targets,
        default_target=target(term_namespace, cloud_ip, "terminal -> cloud default"),
        app_rate_mbps=24.0,
        cache_ttl_s=0.0,
    )
