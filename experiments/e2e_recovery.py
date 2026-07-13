from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from statistics import fmean, median
from typing import Any

from src import report
from src.controller.networking import AgentController
from src.core.models import to_jsonable
from src.e2e.build import build_task_subnet_e2e
from src.e2e.factory import build_rescue_netns_provider
from src.e2e.flowgen import (
    NetnsFlowHealthVerifier,
    NetnsFlowgenManager,
    rescue_flow_gateway_ips,
    rescue_flow_gateway_namespaces,
)
from src.e2e.installers import (
    NetnsGatewayInstaller,
    SimulatedGatewayInstaller,
    rescue_netns_gateway_targets,
)
from src.e2e.recovery import (
    NetnsFaultActuator,
    SUPPORTED_FAULTS,
    SyntheticFaultActuator,
    measure_e2e_recovery,
)
from src.e2e.recovery_planner import (
    LLMRecoveryPlanner,
    OpenAICompatibleRecoveryClient,
    RecoveryPlanner,
)
from src.e2e.verifiers import NetnsTaskSubnetVerifier, SyntheticTaskSubnetVerifier
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


async def run_e2e_recovery_experiment(args: argparse.Namespace) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    initial_builds: list[dict[str, Any]] = []
    full_rebuild_baselines: list[dict[str, Any]] = []
    recovery_planner = _recovery_planner(args)
    warmup: dict[str, Any] = {
        "attempted": False,
        "ok": None,
        "elapsed_ms": 0.0,
        "error": "",
    }
    if recovery_planner is not None and not args.skip_llm_warmup:
        warmup_result = await recovery_planner.warmup(args.llm_warmup_timeout_s)
        warmup = {"attempted": True, **asdict(warmup_result)}

    for run_id in range(1, args.rounds + 1):
        metrics, build_metrics = await _run_one(
            args,
            run_id,
            recovery_mode="incremental",
            recovery_planner=recovery_planner,
        )
        initial_builds.append({"run_id": run_id, "method": "incremental", **asdict(build_metrics)})
        if not args.skip_full_rebuild:
            full, full_build = await _run_one(
                args,
                run_id,
                recovery_mode="full_rebuild",
                recovery_planner=None,
            )
            initial_builds.append({"run_id": run_id, "method": "full_rebuild", **asdict(full_build)})
            full_rebuild_baselines.append(asdict(full))
            metrics = replace(
                metrics,
                full_rebuild_ms=full.elastic_recovery_ms,
                full_rebuild_success=full.incremental_success,
            )
        samples.append(asdict(metrics))
    return {
        "scenario": args.scenario,
        "fault_type": args.fault,
        "rounds": args.rounds,
        "measurement_mode": _measurement_mode(args.install_mode, args.verify_mode),
        "threshold_ms": 3000.0,
        "decision_mode": args.decision_mode,
        "llm": {
            "base_url": args.llm_base_url if args.decision_mode == "llm" else "",
            "model": args.llm_model if args.decision_mode == "llm" else "",
            "timeout_s": args.llm_timeout_s,
            "max_tokens": args.llm_max_tokens,
            "confidence_threshold": args.llm_confidence_threshold,
            "warmup": warmup,
        },
        "summary": _summarize(samples),
        "initial_builds": initial_builds,
        "full_rebuild_baselines": full_rebuild_baselines,
        "samples": samples,
    }


def _measurement_mode(install_mode: str, verify_mode: str) -> str:
    if install_mode == "netns" and verify_mode == "netns":
        return "real-netns"
    if install_mode == "simulated" and verify_mode == "synthetic":
        return "simulated"
    return "mixed"


async def _run_one(
    args: argparse.Namespace,
    run_id: int,
    *,
    recovery_mode: str,
    recovery_planner: RecoveryPlanner | None,
):
    provider = _metric_provider(args)
    controller = AgentController(build_rescue_topology(provider))
    installer = _installer(args, run_id)
    verifier = _verifier(args, provider)
    subnet, build_metrics, _install, _verify = await build_task_subnet_e2e(
        rescue_task(),
        controller,
        provider,
        installer=installer,
        verifier=verifier,
    )
    if not build_metrics.success:
        raise RuntimeError(
            "initial task-subnet build failed: " + " | ".join(build_metrics.errors)
        )

    actuator = _actuator(args, provider)
    flowgen: NetnsFlowgenManager | None = None
    recovery_verifier = verifier
    try:
        if args.verify_mode == "netns" and not args.disable_continuous_flow:
            flowgen = NetnsFlowgenManager(
                gateway_namespaces=rescue_flow_gateway_namespaces(
                    term_namespace=args.term_namespace,
                    edge_namespace=args.edge_namespace,
                    cloud_namespace=args.cloud_namespace,
                ),
                gateway_ips=rescue_flow_gateway_ips(
                    term_ip=args.term_ip,
                    edge_ip=args.edge_ip,
                    cloud_ip=args.cloud_ip,
                ),
                sudo=args.sudo,
                port_base=args.flowgen_port_base,
                startup_s=args.flowgen_startup_s,
            )
            targets = await flowgen.start(subnet)
            recovery_verifier = NetnsFlowHealthVerifier(
                targets,
                sudo=args.sudo,
                command_timeout_s=args.command_timeout_s,
            )
            healthy_before_fault = await recovery_verifier.verify(subnet)
            if not healthy_before_fault.ok:
                raise RuntimeError("continuous flow is not healthy before fault injection")

        metrics = await measure_e2e_recovery(
            scenario=args.scenario,
            run_id=run_id,
            controller=controller,
            subnet=subnet,
            provider=provider,
            installer=installer,
            verifier=recovery_verifier,
            actuator=actuator,
            fault_type=args.fault,
            healthy_windows=args.healthy_windows,
            sample_interval_s=args.sample_interval_s,
            detection_timeout_s=args.detection_timeout_s,
            recovery_timeout_s=args.recovery_timeout_s,
            recovery_mode=recovery_mode,
            decision_mode=(args.decision_mode if recovery_mode == "incremental" else "rule"),
            recovery_planner=recovery_planner,
            detection_source=args.detection_source,
            llm_confidence_threshold=args.llm_confidence_threshold,
            recovery_deadline_ms=args.recovery_deadline_ms,
        )
    finally:
        try:
            await actuator.cleanup()
        finally:
            if flowgen is not None:
                await flowgen.stop()
    return metrics, build_metrics


def _metric_provider(args: argparse.Namespace):
    if args.verify_mode == "netns":
        return build_rescue_netns_provider(
            term_namespace=args.term_namespace,
            edge_namespace=args.edge_namespace,
            cloud_namespace=args.cloud_namespace,
            term_ip=args.term_ip,
            edge_ip=args.edge_ip,
            cloud_ip=args.cloud_ip,
            iperf_port=args.iperf_port,
            ping_count=args.ping_count,
            ping_interval_s=args.ping_interval_s,
            iperf_seconds=args.iperf_seconds,
            command_timeout_s=args.command_timeout_s,
            sudo=args.sudo,
        )
    return SyntheticMetricProvider(base_app_rate_mbps=24.0)


def _installer(args: argparse.Namespace, run_id: int):
    if args.install_mode == "netns":
        return NetnsGatewayInstaller(
            rescue_netns_gateway_targets(
                term_namespace=args.term_namespace,
                edge_namespace=args.edge_namespace,
                cloud_namespace=args.cloud_namespace,
            ),
            sudo=args.sudo,
            command_timeout_s=args.command_timeout_s,
        )
    return SimulatedGatewayInstaller(
        control_rtt_ms=args.control_rtt_ms,
        rule_install_ms=args.rule_install_ms,
        ack_ms=args.ack_ms,
        jitter_ms=args.jitter_ms,
        seed=args.seed + run_id,
    )


def _verifier(args: argparse.Namespace, provider):
    if args.verify_mode == "netns":
        return NetnsTaskSubnetVerifier(provider)
    return SyntheticTaskSubnetVerifier(provider, probe_delay_ms=args.synthetic_probe_delay_ms)


def _actuator(args: argparse.Namespace, provider):
    if args.verify_mode == "netns":
        return NetnsFaultActuator(
            provider=provider,
            severity=args.fault_severity,
            event_namespace=args.event_namespace,
            event_dev=args.event_dev,
            delay_ms=args.netem_delay_ms,
            loss_percent=args.netem_loss_percent,
            sudo=args.sudo,
            command_timeout_s=args.command_timeout_s,
            gateway_namespaces=rescue_flow_gateway_namespaces(
                term_namespace=args.term_namespace,
                edge_namespace=args.edge_namespace,
                cloud_namespace=args.cloud_namespace,
            ),
            gateway_route_targets=rescue_netns_gateway_targets(
                term_namespace=args.term_namespace,
                edge_namespace=args.edge_namespace,
                cloud_namespace=args.cloud_namespace,
            ),
        )
    return SyntheticFaultActuator(provider=provider, severity=args.fault_severity)


def _recovery_planner(args: argparse.Namespace) -> LLMRecoveryPlanner | None:
    if args.decision_mode != "llm":
        return None
    client = OpenAICompatibleRecoveryClient(
        base_url=args.llm_base_url,
        model=args.llm_model,
        api_key=args.llm_api_key or None,
        timeout_s=args.llm_timeout_s,
        temperature=0.0,
        top_p=1.0,
        max_tokens=args.llm_max_tokens,
    )
    return LLMRecoveryPlanner(client=client, timeout_s=args.llm_timeout_s)


def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    elastic = [float(item["elastic_recovery_ms"]) for item in samples]
    e2e = [float(item["e2e_recovery_ms"]) for item in samples]
    full = [
        float(item["full_rebuild_ms"])
        for item in samples
        if item.get("full_rebuild_ms") is not None
    ]
    successes = sum(bool(item["incremental_success"]) for item in samples)
    violations = sum(bool(item["deadline_violated"]) for item in samples)
    llm_samples = [item for item in samples if item.get("decision_mode") == "llm"]
    summary = {
        "success": successes == len(samples),
        "success_rate": successes / len(samples),
        "sample_count": len(samples),
        "elastic_recovery_avg_ms": fmean(elastic),
        "elastic_recovery_p50_ms": median(elastic),
        "elastic_recovery_p95_ms": _percentile(elastic, 95),
        "elastic_recovery_p99_ms": _percentile(elastic, 99),
        "elastic_recovery_min_ms": min(elastic),
        "elastic_recovery_max_ms": max(elastic),
        "e2e_recovery_avg_ms": fmean(e2e),
        "e2e_recovery_median_ms": median(e2e),
        "e2e_recovery_p95_ms": _percentile(e2e, 95),
        "e2e_recovery_min_ms": min(e2e),
        "e2e_recovery_max_ms": max(e2e),
        "deadline_violation_rate": violations / len(samples),
        "meets_3s_p95": successes == len(samples)
        and _percentile(elastic, 95) <= 3000.0,
    }
    if llm_samples:
        summary.update(
            {
                "model_timeout_rate": sum(bool(item["model_timeout"]) for item in llm_samples)
                / len(llm_samples),
                "fallback_rate": sum(item["decision_source"] == "rule_fallback" for item in llm_samples)
                / len(llm_samples),
                "llm_plan_valid_rate": sum(bool(item["llm_plan_valid"]) for item in llm_samples)
                / len(llm_samples),
                "llm_plan_adoption_rate": sum(bool(item["llm_plan_adopted"]) for item in llm_samples)
                / len(llm_samples),
                "model_analysis_avg_ms": fmean(
                    float(item["model_analysis_ms"]) for item in llm_samples
                ),
                "model_analysis_p95_ms": _percentile(
                    [float(item["model_analysis_ms"]) for item in llm_samples], 95
                ),
            }
        )
    if full:
        summary.update(
            {
                "full_rebuild_success": all(
                    bool(item["full_rebuild_success"])
                    for item in samples
                    if item.get("full_rebuild_success") is not None
                ),
                "full_rebuild_avg_ms": fmean(full),
                "full_rebuild_p95_ms": _percentile(full, 95),
            }
        )
    return summary


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def render_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [report.banner("任务通信子网弹性恢复时延")]
    lines.append(report.section("测量口径"))
    lines.append(
        report.kv_block(
            [
                ("主指标起点", "任意检测器确认异常并生成检测事件 t_detected"),
                ("终点", "关键业务边连续达到指定健康窗口数"),
                ("弹性恢复", "elastic_recovery_ms = t_restore - t_detected（3 秒指标）"),
                ("检测参考", "fault_detect_ms 单独报告，不计入弹性恢复时延"),
                ("故障到恢复", "e2e_recovery_ms = t_restore - t_fault（参考）"),
                ("估计中断", "estimated_interruption_ms 仅为 CostModel 估计，不是实测恢复"),
                ("故障", result["fault_type"]),
                ("决策模式", result["decision_mode"]),
            ]
        )
    )
    lines.append(report.section("统计结果"))
    lines.append(
        report.kv_block(
            [
                ("成功", "是" if summary["success"] else "否"),
                ("弹性平均/P95", f"{summary['elastic_recovery_avg_ms']:.3f} / {summary['elastic_recovery_p95_ms']:.3f} ms"),
                ("弹性 P50/P99", f"{summary['elastic_recovery_p50_ms']:.3f} / {summary['elastic_recovery_p99_ms']:.3f} ms"),
                ("故障到恢复平均/P95", f"{summary['e2e_recovery_avg_ms']:.3f} / {summary['e2e_recovery_p95_ms']:.3f} ms"),
                (
                    "全量重建平均/P95",
                    f"{summary['full_rebuild_avg_ms']:.3f} / {summary['full_rebuild_p95_ms']:.3f} ms"
                    if "full_rebuild_avg_ms" in summary
                    else "未执行",
                ),
                (
                    "3 秒指标",
                    ("满足" if summary["meets_3s_p95"] else "不满足")
                    + ("（真实 netns 测量）" if result["measurement_mode"] == "real-netns" else "（模拟/混合配置，仅供回归）"),
                ),
                ("超限率", f"{summary['deadline_violation_rate'] * 100:.2f}%"),
            ]
        )
    )
    if result["decision_mode"] == "llm":
        warmup = result["llm"]["warmup"]
        lines.append(report.section("模型决策"))
        lines.append(
            report.kv_block(
                [
                    ("模型", result["llm"]["model"]),
                    ("推理超时", f"{result['llm']['timeout_s']:.3f} s"),
                    ("预热", ("成功" if warmup.get("ok") else "失败/跳过") + f"，{warmup.get('elapsed_ms', 0.0):.3f} ms（不计时）"),
                    ("模型超时率", f"{summary.get('model_timeout_rate', 0.0) * 100:.2f}%"),
                    ("规则回退率", f"{summary.get('fallback_rate', 0.0) * 100:.2f}%"),
                    ("方案采纳率", f"{summary.get('llm_plan_adoption_rate', 0.0) * 100:.2f}%"),
                ]
            )
        )
    lines.append(report.section("分项明细"))
    lines.append(
        report.table(
            headers=["run", "detect*", "model", "validate", "apply", "install", "restore", "elastic", "e2e*", "来源", "策略", "成功"],
            rows=[
                [
                    item["run_id"],
                    f"{item['fault_detect_ms']:.3f}",
                    f"{item['model_analysis_ms']:.3f}",
                    f"{item['controller_validation_ms']:.3f}",
                    f"{item['controller_apply_ms']:.3f}",
                    f"{item['delta_install_ms']:.3f}",
                    f"{item['business_restore_ms']:.3f}",
                    f"{item['elastic_recovery_ms']:.3f}",
                    f"{item['e2e_recovery_ms']:.3f}",
                    item["decision_source"],
                    item["applied_strategy"],
                    "Y" if item["incremental_success"] else "N",
                ]
                for item in result["samples"][:20]
            ],
        )
    )
    errors = [error for item in result["samples"] for error in item.get("errors", ())]
    if errors:
        lines.append(report.section("错误"))
        lines.extend(report.bullet(error) for error in errors[:10])
    return "\n".join(lines)


def write_csv(path: str | Path, samples: list[dict[str, Any]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "scenario",
        "fault_type",
        "run_id",
        "fault_detect_ms",
        "model_analysis_ms",
        "controller_validation_ms",
        "controller_apply_ms",
        "recovery_decision_ms",
        "delta_install_ms",
        "business_restore_ms",
        "elastic_recovery_ms",
        "e2e_recovery_ms",
        "estimated_interruption_ms",
        "changed_agent_count",
        "changed_edge_count",
        "changed_gateway_count",
        "full_rebuild_ms",
        "incremental_success",
        "full_rebuild_success",
        "healthy_windows",
        "verify_attempts",
        "strategy",
        "detection_source",
        "decision_mode",
        "decision_source",
        "diagnosed_fault_type",
        "model_strategy",
        "applied_strategy",
        "model_timeout",
        "llm_plan_valid",
        "llm_plan_adopted",
        "fallback_reason",
        "deadline_violated",
        "remediation",
        "install_mode",
        "verify_mode",
        "errors",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sample in samples:
            writer.writerow({**sample, "errors": " | ".join(sample.get("errors", ()))})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure fault-to-business-restoration latency.")
    parser.add_argument("--scenario", choices=("rescue",), default="rescue")
    parser.add_argument("--fault", choices=SUPPORTED_FAULTS, default="link_degrade")
    parser.add_argument("--install-mode", choices=("simulated", "netns"), default="simulated")
    parser.add_argument("--verify-mode", choices=("synthetic", "netns"), default="synthetic")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--healthy-windows", type=int, default=3)
    parser.add_argument("--sample-interval-s", type=float, default=0.2)
    parser.add_argument("--detection-timeout-s", type=float, default=10.0)
    parser.add_argument("--recovery-timeout-s", type=float, default=10.0)
    parser.add_argument("--recovery-deadline-ms", type=float, default=3000.0)
    parser.add_argument("--detection-source", default="auto")
    parser.add_argument("--decision-mode", choices=("rule", "llm"), default="rule")
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("RECOVERY_LLM_BASE_URL", "http://127.0.0.1:8001/v1"),
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("RECOVERY_LLM_MODEL", "qwen-recovery"),
    )
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("RECOVERY_LLM_API_KEY", ""),
    )
    parser.add_argument("--llm-timeout-s", type=float, default=1.0)
    parser.add_argument("--llm-max-tokens", type=int, default=32)
    parser.add_argument("--llm-confidence-threshold", type=float, default=0.7)
    parser.add_argument("--llm-warmup-timeout-s", type=float, default=30.0)
    parser.add_argument("--skip-llm-warmup", action="store_true")
    parser.add_argument(
        "--skip-full-rebuild",
        action="store_true",
        help="Skip the independently executed full-rebuild recovery baseline.",
    )
    parser.add_argument("--fault-severity", type=float, default=1.0)
    parser.add_argument("--netem-delay-ms", type=float, default=160.0)
    parser.add_argument("--netem-loss-percent", type=float, default=10.0)
    parser.add_argument("--event-namespace", default="h-router")
    parser.add_argument("--event-dev", default="rt-cloud0")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--control-rtt-ms", type=float, default=10.0)
    parser.add_argument("--rule-install-ms", type=float, default=30.0)
    parser.add_argument("--ack-ms", type=float, default=5.0)
    parser.add_argument("--jitter-ms", type=float, default=5.0)
    parser.add_argument("--synthetic-probe-delay-ms", type=float, default=10.0)
    parser.add_argument("--flowgen-port-base", type=int, default=9400)
    parser.add_argument("--flowgen-startup-s", type=float, default=0.5)
    parser.add_argument(
        "--disable-continuous-flow",
        action="store_true",
        help="Use repeated ping/iperf verification instead of persistent flowgen + ping/ss.",
    )
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--term-namespace", default="h-term")
    parser.add_argument("--edge-namespace", default="h-edge")
    parser.add_argument("--cloud-namespace", default="h-cloud")
    parser.add_argument("--term-ip", default="10.10.1.2")
    parser.add_argument("--edge-ip", default="10.10.2.2")
    parser.add_argument("--cloud-ip", default="10.10.3.2")
    parser.add_argument("--iperf-port", type=int, default=5201)
    parser.add_argument("--ping-count", type=int, default=3)
    parser.add_argument("--ping-interval-s", type=float, default=0.2)
    parser.add_argument("--iperf-seconds", type=int, default=1)
    parser.add_argument("--command-timeout-s", type=float, default=8.0)
    parser.add_argument("--json-output")
    parser.add_argument("--csv-output")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.rounds <= 0:
        raise SystemExit("--rounds must be positive")
    if args.llm_timeout_s <= 0.0 or args.llm_warmup_timeout_s <= 0.0:
        raise SystemExit("LLM timeouts must be positive")
    if args.llm_max_tokens <= 0:
        raise SystemExit("--llm-max-tokens must be positive")
    if not 0.0 <= args.llm_confidence_threshold <= 1.0:
        raise SystemExit("--llm-confidence-threshold must be between 0 and 1")
    result = asyncio.run(run_e2e_recovery_experiment(args))
    if args.csv_output:
        write_csv(args.csv_output, result["samples"])
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(to_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(render_report(to_jsonable(result)))


if __name__ == "__main__":
    main()
