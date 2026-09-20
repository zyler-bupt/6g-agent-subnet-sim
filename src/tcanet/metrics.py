"""Evaluation metrics for TCANet (paper Sec. V, Eq. 10).

* **Formation latency** — from the upper-level confirmation of ``T_m`` to
  the acceptance of the initial subnet version.
* **Recovery latency** — from the runtime event to the acceptance of a
  repaired version; only successful trials are counted.
* **Modification ratio** ``Mod_m = N^changed_m / N^installed_m`` — the
  fraction of installed records (path, forwarding-table, binding) that a
  reconfiguration changed, relative to the pre-event installed state; a
  record that a replacement overwrites is counted once, not twice.
* **Feasible decision rate** — fraction of evaluation trials in which a
  feasible joint decision exists, as a function of the load factor
  ``γ_m`` (Eq. 9, ``ρ0 = 0.65``).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.tcanet.binding import BindingTable
from src.tcanet.subnet import ForwardingEntry, PathRecord, SubnetState

# Reference utilization used by the load factor gamma (Eq. 9).
REFERENCE_UTILIZATION = 0.65


def load_factor(
    offered_load_mbps: float, reference_load_mbps: float
) -> float:
    """``γ_m`` (Eq. 9): offered load relative to the reference point ``ρ0``."""
    if reference_load_mbps <= 0.0:
        return 0.0
    return offered_load_mbps / reference_load_mbps


def _ft_pairs(
    forwarding: dict[str, ForwardingEntry],
) -> set[tuple[str, str]]:
    """Distinct (dep, gateway) forwarding records — a reroute that swaps a
    rule at the same gateway is one replaced record, not two."""
    return {(entry.dep_id, entry.gateway_id) for entry in forwarding.values()}


def _binding_keys(table: BindingTable) -> set[str]:
    return set(table.records)


@dataclass(frozen=True)
class ModificationStats:
    """``N^installed``, ``N^changed`` and ``Mod_m`` for one reconfiguration."""

    installed_paths: int
    installed_forwarding: int
    installed_bindings: int
    changed_paths: int
    changed_forwarding: int
    changed_bindings: int

    @property
    def installed(self) -> int:
        return (
            self.installed_paths
            + self.installed_forwarding
            + self.installed_bindings
        )

    @property
    def changed(self) -> int:
        return (
            self.changed_paths
            + self.changed_forwarding
            + self.changed_bindings
        )

    @property
    def ratio(self) -> float:
        """``Mod_m`` (Eq. 10); 0.0 when nothing was installed."""
        if self.installed == 0:
            return 0.0
        return self.changed / self.installed


def modification_stats(
    before: SubnetState,
    after: SubnetState,
) -> ModificationStats:
    """Compare two subnet versions over path/FT/binding records (Eq. 10).

    Path records are compared per dependency, forwarding records per
    (dependency, gateway) pair and bindings per dependency, so a
    replacement counts once.
    """
    changed_paths = {
        dep_id
        for dep_id in set(before.paths) | set(after.paths)
        if _path_signature(before.paths.get(dep_id))
        != _path_signature(after.paths.get(dep_id))
    }
    old_pairs = _ft_pairs(before.forwarding)
    new_pairs = _ft_pairs(after.forwarding)
    changed_ft: set[tuple[str, str]] = set()
    for pair in old_pairs ^ new_pairs:
        changed_ft.add(pair)
    for pair in old_pairs & new_pairs:
        old_rules = {
            e.rule_id: e
            for e in before.forwarding.values()
            if (e.dep_id, e.gateway_id) == pair
        }
        new_rules = {
            e.rule_id: e
            for e in after.forwarding.values()
            if (e.dep_id, e.gateway_id) == pair
        }
        if old_rules != new_rules:
            changed_ft.add(pair)

    changed_bindings = before.bindings.diff(after.bindings)
    return ModificationStats(
        installed_paths=len(before.paths),
        installed_forwarding=len(old_pairs),
        installed_bindings=len(_binding_keys(before.bindings)),
        changed_paths=len(changed_paths),
        changed_forwarding=len(changed_ft),
        changed_bindings=len(changed_bindings),
    )


def _path_signature(record: PathRecord | None) -> tuple[str, ...]:
    return record.gateway_path if record is not None else ()


@dataclass(frozen=True)
class TrialRecord:
    """One evaluation trial for success/feasibility accounting."""

    task_id: str
    recovered: bool
    recovery_latency_ms: float | None  # None on failed trials


@dataclass(frozen=True)
class MetricSummary:
    trials: int
    successes: int
    mean_recovery_latency_ms: float | None
    mean_modification_ratio: float

    @property
    def success_rate(self) -> float:
        if self.trials == 0:
            return 0.0
        return self.successes / self.trials


def summarize(
    trials: tuple[TrialRecord, ...],
    modification_ratios: tuple[float, ...] = (),
) -> MetricSummary:
    """Aggregate recovery success rate and mean metrics over trials.

    Recovery latency averages over successful trials only (Sec. V).
    """
    successes = tuple(item for item in trials if item.recovered)
    latencies = [
        item.recovery_latency_ms
        for item in successes
        if item.recovery_latency_ms is not None
    ]
    mean_latency = (
        sum(latencies) / len(latencies) if latencies else None
    )
    mean_ratio = (
        sum(modification_ratios) / len(modification_ratios)
        if modification_ratios
        else 0.0
    )
    return MetricSummary(
        trials=len(trials),
        successes=len(successes),
        mean_recovery_latency_ms=mean_latency,
        mean_modification_ratio=mean_ratio,
    )
