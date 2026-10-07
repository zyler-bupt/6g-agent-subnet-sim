"""Algorithm 1 log entries for the controller terminal, UI and timeline.

Each entry is a JSON-serializable dict ``{"step", "title", "lines", "level"}``
whose ``step`` names the Algorithm 1 stage it documents.
"""
from __future__ import annotations

from collections import Counter

from rich.text import Text

from src.tcanet.closure import ClosureResult, RuntimeEvent
from src.tcanet.metrics import ModificationStats
from src.tcanet.prototype.addressing import rule_commands
from src.tcanet.scenario import event_description
from src.tcanet.selection import SelectionTrace
from src.tcanet.spec import TaskSpecification
from src.tcanet.subnet import SubnetState
from src.tcanet.verify import DepObservation, WindowResult

_ADMISSIBILITY = ("write_conflict", "executor_unavailable", "precondition_failed", "binding_offline")
_STYLE = {"info": "white", "ok": "bold green", "warn": "bold yellow", "fail": "bold red"}
_STEP_STYLE = {"event": "bold red", "scope": "cyan", "select": "magenta", "apply": "blue",
               "assess": "yellow", "rollback": "bold red", "commit": "bold green",
               "formation": "bold cyan", "withdraw": "dim"}


def entry(step: str, title: str, lines: list[str] | None = None, level: str = "info",
          **data) -> dict:
    """One log entry; ``data`` carries machine-readable fields for the UI."""
    return {"step": step, "title": title, "lines": list(lines or []), "level": level, "data": data}


def _fmt(deps) -> str:
    return "{" + ", ".join(sorted(deps)) + "}"


def event_entry(event: RuntimeEvent, evidence_ts: float, detected_ts: float) -> dict:
    return entry("event", f"Event: {event_description(event)}",
                 [f"first agent evidence → event: {(detected_ts - evidence_ts) * 1000:.0f} ms"], "fail",
                 kind=event.kind, evidence_ts=evidence_ts)


def scope_entry(closure: ClosureResult, all_deps: list[str]) -> dict:
    lines = ["E^aff_0 = " + _fmt(closure.initial) + "   (Eq. 14)"]
    lines += [f"  {dep}: {reason}" for dep, reason in sorted(closure.initial.items())]
    for round_ in closure.rounds:
        added = ", ".join(f"{dep} ← {reason}" for dep, reason in sorted(round_.added.items()))
        lines.append(f"round {round_.index}: + {added}   (Eq. 15)")
    untouched = sorted(set(all_deps) - set(closure.final))
    lines.append(f"fixed point E^aff = {_fmt(closure.final)}; retained unchanged: {_fmt(untouched)}   (Eq. 16)")
    return entry("scope", "Determine scope", lines, affected=sorted(closure.final))


def selection_entry(trace: SelectionTrace) -> dict:
    evaluations = trace.evaluations
    admissible = [e for e in evaluations
                  if not any(v.startswith(_ADMISSIBILITY) for v in e.feasibility.violations)]
    feasible = [e for e in evaluations if e.feasibility.feasible]
    lines = [f"|C| = {len(evaluations)}   |C^ad| = {len(admissible)}   |C^feas| = {len(feasible)}   (Eq. 8)"]
    reasons = Counter(v for e in admissible if not e.feasibility.feasible for v in e.feasibility.violations)
    for violation, count in reasons.most_common(3):
        lines.append(f"  rejected ×{count}: {violation}")
    if trace.selected is None:
        lines.append("no feasible joint configuration")
        return entry("select", "Joint feasibility", lines, "fail")
    lines.append(f"J* = {trace.stage1_min_v:.3f} attained by {len(trace.stage1_survivors)} configs   (Eq. 11)")
    by_scope = Counter(e.modification_scope for e in trace.stage1_survivors)
    lines.append("M over J*-optimal configs: " + ", ".join(
        f"M={scope}×{count}" for scope, count in sorted(by_scope.items())[:4]))
    lines.append(f"c* = {trace.selected.label}  (min M = {trace.stage2_min_r}, Eq. 12)")
    if trace.tiebreak_note:
        lines.append(f"  tie: {trace.tiebreak_note}")
    return entry("select", "Joint feasibility → minimize J, then M", lines)


def apply_line(payload: dict, ok: bool, detail: str) -> dict:
    kind, params = payload["kind"], payload["params"]
    undo = payload["action_id"].startswith("undo:")
    if kind.endswith("_rule"):
        op = "add" if kind == "install_rule" else "del"
        hop = "local" if params.get("mode") == "local_delivery" else f"→{params.get('next_hop_gateway')}"
        text = f"{'+' if op == 'add' else '−'}FT {params['dep_id']}@{params['gateway']} {hop}"
        if detail.startswith(("withdrawn", "not restored")):
            text += f"   ({detail})"
        else:
            text += "   " + rule_commands(params, op)[-1].text()
    elif kind == "bind_support":
        text = f"Φ {payload['target']}.{params['role']} → {payload['executor']}"
    else:
        shown = {k: v for k, v in params.items() if k not in ("app_agent",)}
        text = f"u {payload['value']} {payload['target']} {shown} @ {payload['executor']}"
    if detail == "replaced in place":
        text += "   (replaced in place)"
    text = ("undo " if undo else "") + text + ("" if ok else f"   ✗ {detail}")
    return entry("rollback" if undo else "apply", text, [], "info" if ok else "fail")


def assess_entry(window: WindowResult, observations: tuple[DepObservation, ...], task: TaskSpecification) -> dict:
    lines = []
    deps = {dep.dep_id: dep for dep in task.dag.dependencies}
    for obs in observations:
        req = task.requirements_for(deps[obs.dep_id])
        g_thr = (req.min_throughput_mbps - obs.throughput_mbps) / req.min_throughput_mbps
        g_delay = (obs.delay_ms - req.max_delay_ms) / req.max_delay_ms
        g_loss = (obs.loss_rate - req.max_loss_rate) / req.max_loss_rate
        marks = ["✓" if g <= 0 else "✗" for g in (g_thr, g_delay, g_loss)]
        lines.append(
            f"{obs.dep_id}: {obs.throughput_mbps:5.1f} Mbps {marks[0]}  owd {obs.delay_ms:5.1f} ms {marks[1]}  "
            f"loss {obs.loss_rate * 100:4.1f}% {marks[2]}   g = ({g_thr:+.2f}, {g_delay:+.2f}, {g_loss:+.2f})"
        )
    for dep in window.pending:
        lines.append(f"{dep}: no measurement in window")
    verdict = "PASS" if window.accepted else "FAIL " + ", ".join(window.violations or window.pending)
    return entry("assess", f"Assess (Eq. 5–6): {verdict}", lines, "ok" if window.accepted else "fail")


def rollback_entry(version: int, restored: int) -> dict:
    return entry("rollback", f"Rollback v{version} → v{restored}; refresh s_m; C ← C \\ {{c*}}", [], "warn")


def commit_entry(subnet: SubnetState, latency_ms: float, stats: ModificationStats | None, label: str) -> dict:
    lines = [f"{label} latency {latency_ms:.0f} ms"]
    if stats is not None:
        lines.append(
            f"M_m = {stats.changed} (ΔΠ={stats.changed_paths}, ΔFT={stats.changed_forwarding}, "
            f"ΔΦ={stats.changed_bindings})   Mod = {stats.ratio * 100:.0f}% of {stats.installed} records"
        )
    lines += [f"π {dep}: {'→'.join(path.gateway_path)}" for dep, path in sorted(subnet.paths.items())]
    return entry("commit", f"Commit v{subnet.version}", lines, "ok", version=subnet.version,
                 latency_ms=latency_ms)


def render(item: dict) -> Text:
    text = Text()
    text.append(f"[{item['step']:^9}] ", style=_STEP_STYLE.get(item["step"], "white"))
    text.append(item["title"], style=_STYLE.get(item["level"], "white"))
    for line in item["lines"]:
        text.append("\n            " + line, style="dim" if item["level"] == "info" else "")
    return text
