from __future__ import annotations

import heapq
from dataclasses import dataclass, replace
from typing import Iterable


@dataclass(frozen=True)
class TrafficEngineeringLink:
    link_id: str
    source: str
    target: str
    available_bandwidth_mbps: float
    delay_ms: float
    te_cost: float
    up: bool = True


@dataclass(frozen=True)
class CspfRequest:
    flow_id: str
    source: str
    destination: str
    required_bandwidth_mbps: float
    maximum_delay_ms: float
    metric: str = "delay"


@dataclass(frozen=True)
class CspfResult:
    flow_id: str
    feasible: bool
    path: tuple[str, ...]
    link_ids: tuple[str, ...]
    total_delay_ms: float
    total_cost: float
    pruned_link_ids: tuple[str, ...]
    failure_reason: str = ""


class CspfSolver:
    """Constrained SPF over network-layer traffic-engineering state only."""

    def solve(
        self,
        links: Iterable[TrafficEngineeringLink],
        request: CspfRequest,
    ) -> CspfResult:
        if request.required_bandwidth_mbps < 0.0:
            raise ValueError("required bandwidth must be non-negative")
        if request.maximum_delay_ms < 0.0:
            raise ValueError("maximum delay must be non-negative")
        if request.metric not in {"delay", "te_cost"}:
            raise ValueError(f"unsupported CSPF metric: {request.metric}")
        materialized = tuple(links)
        pruned = tuple(
            link.link_id
            for link in materialized
            if not link.up
            or link.available_bandwidth_mbps + 1e-9
            < request.required_bandwidth_mbps
        )
        usable = tuple(link for link in materialized if link.link_id not in pruned)
        adjacency: dict[str, list[TrafficEngineeringLink]] = {}
        for link in usable:
            if link.delay_ms < 0.0 or link.te_cost < 0.0:
                raise ValueError(f"negative CSPF link metric: {link.link_id}")
            adjacency.setdefault(link.source, []).append(link)
        for edges in adjacency.values():
            edges.sort(key=lambda item: (item.target, item.link_id))

        queue: list[tuple[float, float, tuple[str, ...], str, tuple[str, ...]]] = [
            (0.0, 0.0, (request.source,), request.source, ())
        ]
        best: dict[str, tuple[float, float, tuple[str, ...]]] = {}
        while queue:
            metric_cost, delay, path, node, path_links = heapq.heappop(queue)
            signature = (metric_cost, delay, path)
            if node in best and best[node] <= signature:
                continue
            best[node] = signature
            if node == request.destination:
                if delay > request.maximum_delay_ms + 1e-9:
                    break
                return CspfResult(
                    flow_id=request.flow_id,
                    feasible=True,
                    path=path,
                    link_ids=path_links,
                    total_delay_ms=delay,
                    total_cost=metric_cost,
                    pruned_link_ids=pruned,
                )
            for link in adjacency.get(node, ()):
                if link.target in path:
                    continue
                next_delay = delay + link.delay_ms
                weight = link.delay_ms if request.metric == "delay" else link.te_cost
                heapq.heappush(
                    queue,
                    (
                        metric_cost + weight,
                        next_delay,
                        (*path, link.target),
                        link.target,
                        (*path_links, link.link_id),
                    ),
                )
        reason = "no_bandwidth_feasible_path"
        if usable and request.source in adjacency:
            reason = "no_path_within_delay_bound"
        return CspfResult(
            flow_id=request.flow_id,
            feasible=False,
            path=(),
            link_ids=(),
            total_delay_ms=0.0,
            total_cost=0.0,
            pruned_link_ids=pruned,
            failure_reason=reason,
        )


def reserve_path(
    links: Iterable[TrafficEngineeringLink],
    result: CspfResult,
    bandwidth_mbps: float,
) -> tuple[TrafficEngineeringLink, ...]:
    if not result.feasible:
        raise ValueError("cannot reserve an infeasible CSPF result")
    selected = set(result.link_ids)
    return tuple(
        replace(
            link,
            available_bandwidth_mbps=max(
                0.0,
                link.available_bandwidth_mbps - bandwidth_mbps,
            ),
        )
        if link.link_id in selected
        else link
        for link in links
    )


def links_from_dicts(rows: Iterable[dict[str, object]]) -> tuple[TrafficEngineeringLink, ...]:
    return tuple(
        TrafficEngineeringLink(
            link_id=str(row["link_id"]),
            source=str(row["source"]),
            target=str(row["target"]),
            available_bandwidth_mbps=float(row["available_bandwidth_mbps"]),
            delay_ms=float(row["delay_ms"]),
            te_cost=float(row.get("te_cost", row["delay_ms"])),
            up=bool(row.get("up", True)),
        )
        for row in rows
    )
