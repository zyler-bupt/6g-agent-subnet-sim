#!/usr/bin/env python3
"""Audit Fig.1 qdisc coverage and per-flow directional bottleneck tolerance."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


def expected_endpoints(num_agents: int, num_gateways: int) -> set[str]:
    endpoints = {f"agent-{index}:eth0" for index in range(num_agents)}
    endpoints.update(
        f"gateway-{index % num_gateways}:ga{index}" for index in range(num_agents)
    )
    endpoints.update(f"gateway-{index}:up0" for index in range(num_gateways))
    endpoints.update(f"outer:og{index}" for index in range(num_gateways))
    return endpoints


def directional_path_endpoints(
    source_index: int, target_index: int, num_gateways: int
) -> tuple[str, ...]:
    source_gateway = source_index % num_gateways
    target_gateway = target_index % num_gateways
    endpoints = [f"agent-{source_index}:eth0"]
    if source_gateway != target_gateway:
        endpoints.extend(
            (f"gateway-{source_gateway}:up0", f"outer:og{target_gateway}")
        )
    endpoints.append(f"gateway-{target_gateway}:ga{target_index}")
    return tuple(endpoints)


def audit(config: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    num_gateways = int(config["task"]["num_gateways"])
    tolerance = config["audit"]["path_bottleneck"]
    relative_tolerance = float(tolerance["relative_tolerance"])
    minimum_tolerance = float(tolerance["short_tcp_min_tolerance_mbps"])
    profiles_by_run: dict[str, dict[str, dict[str, Any]]] = {}
    iperf_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    traffic_control_events = 0
    for event in events:
        stage = event.get("stage")
        run_id = str(event.get("run_id", ""))
        if stage == "TRAFFIC_CONTROL_FINISHED":
            traffic_control_events += 1
            profiles = event.get("details", {}).get("profiles", [])
            profiles_by_run[run_id] = {
                str(profile["endpoint"]): profile for profile in profiles
            }
            expected = expected_endpoints(int(event["num_agents"]), num_gateways)
            actual = set(profiles_by_run[run_id])
            if actual != expected:
                errors.append(
                    f"{run_id}: qdisc endpoint mismatch missing={sorted(expected - actual)} "
                    f"unexpected={sorted(actual - expected)}"
                )
        elif stage == "IPERF3_COMMAND_RESULT":
            iperf_rows.append(event)
    if traffic_control_events == 0:
        errors.append("missing TRAFFIC_CONTROL_FINISHED event coverage")
    if not iperf_rows:
        errors.append("missing IPERF3_COMMAND_RESULT event coverage")
    checked_flows = 0
    timeout_records = 0
    for event in iperf_rows:
        details = event["details"]
        if details.get("timeout"):
            timeout_records += 1
            continue
        run_id = str(event["run_id"])
        profiles = profiles_by_run.get(run_id)
        if profiles is None:
            errors.append(f"{run_id}: missing traffic-control profiles for iPerf3 result")
            continue
        path = directional_path_endpoints(
            int(details["source_index"]), int(details["target_index"]), num_gateways
        )
        missing = [endpoint for endpoint in path if endpoint not in profiles]
        if missing:
            errors.append(f"{run_id}:{details['edge_id']}: unprofiled path endpoints {missing}")
            continue
        bottleneck = min(float(profiles[endpoint]["bandwidth_mbps"]) for endpoint in path)
        allowed = bottleneck + max(minimum_tolerance, bottleneck * relative_tolerance)
        throughput = float(details.get("throughput_mbps", 0.0))
        checked_flows += 1
        if throughput > allowed + 1e-9:
            errors.append(
                f"{run_id}:{details['edge_id']}: throughput={throughput:.6f}Mbps "
                f"exceeds bottleneck={bottleneck:.6f}Mbps plus tolerance={allowed - bottleneck:.6f}Mbps"
            )
    return {
        "passed": not errors,
        "runs_with_profiles": len(profiles_by_run),
        "traffic_control_events": traffic_control_events,
        "iperf_timeout_records": timeout_records,
        "iperf_non_timeout_attempts_checked": checked_flows,
        "relative_tolerance": relative_tolerance,
        "short_tcp_min_tolerance_mbps": minimum_tolerance,
        "errors": errors,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


FORMAL_TASK_SIZES = (4, 8, 12, 16, 20)
FORMAL_SEEDS = tuple(range(50))


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _configuration_sha256(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def formal_schedule() -> list[dict[str, int]]:
    """Return the frozen 10-block, five-size Latin schedule in run order."""
    schedule: list[dict[str, int]] = []
    for block_index in range(10):
        rotated_sizes = FORMAL_TASK_SIZES[block_index % len(FORMAL_TASK_SIZES) :] + FORMAL_TASK_SIZES[: block_index % len(FORMAL_TASK_SIZES)]
        for order_position, num_agents in enumerate(rotated_sizes):
            for seed in range(block_index * 5, (block_index + 1) * 5):
                schedule.append(
                    {
                        "run_sequence": len(schedule) + 1,
                        "block_index": block_index,
                        "order_position": order_position,
                        "num_agents": num_agents,
                        "seed": seed,
                    }
                )
    return schedule


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def _artifact_paths(config_path: Path, results_dir: Path) -> dict[str, Path]:
    repository = Path(__file__).resolve().parents[1]
    raw = results_dir / "raw"
    processed = results_dir / "processed"
    return {
        "formal_config": config_path,
        "experiment_implementation": repository / "experiments" / "exp1_netns_verified_formation.py",
        "audit_script": Path(__file__).resolve(),
        "aggregate_script": repository / "scripts" / "aggregate_exp1_netns.py",
        "raw_runs": raw / "runs.csv",
        "raw_events": raw / "events.jsonl",
        "measurement_scope": raw / "measurement_scope.json",
        "processed_summary": processed / "summary.csv",
        "processed_failures": processed / "failures.csv",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_versions() -> dict[str, dict[str, object]]:
    commands = {
        "kernel": ("uname", "-r"),
        "ip": ("ip", "-V"),
        "tc": ("tc", "-V"),
        "ping": ("ping", "-V"),
        "iperf3": ("iperf3", "--version"),
    }
    captured: dict[str, dict[str, object]] = {}
    for name, command in commands.items():
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=2)
            captured[name] = {
                "command": list(command),
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            captured[name] = {
                "command": list(command),
                "error": f"{type(error).__name__}: {error}",
            }
    return captured


def _normalize_schedule(schedule: object) -> list[dict[str, int]] | None:
    if not isinstance(schedule, list):
        return None
    normalized: list[dict[str, int]] = []
    fields = ("run_sequence", "block_index", "order_position", "num_agents", "seed")
    try:
        for item in schedule:
            if not isinstance(item, dict):
                return None
            normalized.append({field: int(item[field]) for field in fields})
    except (KeyError, TypeError, ValueError):
        return None
    return normalized


def _recompute_processed_artifacts(runs_path: Path) -> dict[str, bytes]:
    try:
        from scripts.aggregate_exp1_netns import aggregate
    except ModuleNotFoundError:
        from aggregate_exp1_netns import aggregate  # type: ignore[no-redef]
    with tempfile.TemporaryDirectory() as directory:
        output_dir = Path(directory)
        aggregate(runs_path, output_dir)
        return {
            "processed_summary": (output_dir / "summary.csv").read_bytes(),
            "processed_failures": (output_dir / "failures.csv").read_bytes(),
        }


def audit_formal_results(
    config_path: Path,
    results_dir: Path,
    *,
    manifest_path: Path | None = None,
    create_manifest: bool = False,
) -> dict[str, Any]:
    """Audit the frozen formal Fig.1 artifact set without modifying it."""
    artifact_paths = _artifact_paths(config_path, results_dir)
    errors: list[str] = []
    missing = [name for name, path in artifact_paths.items() if not path.is_file()]
    if missing:
        return {"passed": False, "errors": [f"missing formal artifact(s): {', '.join(missing)}"]}

    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    _, rows = _read_csv(artifact_paths["raw_runs"])
    events = _read_jsonl(artifact_paths["raw_events"])
    scope = json.loads(artifact_paths["measurement_scope"].read_text(encoding="utf-8"))
    expected_schedule = formal_schedule()
    expected_grid = {(item["num_agents"], item["seed"]) for item in expected_schedule}
    expected_by_sequence = {item["run_sequence"]: item for item in expected_schedule}

    if len(rows) != len(expected_schedule):
        errors.append(f"formal grid row count={len(rows)} expected={len(expected_schedule)}")
    actual_grid: list[tuple[int, int]] = []
    actual_sequences: list[int] = []
    for row in rows:
        run_id = row.get("run_id", "<unknown>")
        try:
            grid_item = (int(row["num_agents"]), int(row["seed"]))
            sequence = int(row["run_sequence"])
            actual_grid.append(grid_item)
            actual_sequences.append(sequence)
            expected = expected_by_sequence.get(sequence)
            observed = {
                "run_sequence": sequence,
                "block_index": int(row["block_index"]),
                "order_position": int(row["order_position"]),
                "num_agents": grid_item[0],
                "seed": grid_item[1],
            }
            if expected != observed:
                errors.append(f"{run_id}: formal schedule drift")
        except (KeyError, TypeError, ValueError):
            errors.append(f"{run_id}: malformed formal schedule row")
    if set(actual_grid) != expected_grid or len(actual_grid) != len(set(actual_grid)):
        errors.append("formal grid is not the exact 5x50 size-by-seed Cartesian product")
    if sorted(actual_sequences) != list(range(1, len(expected_schedule) + 1)):
        errors.append("formal schedule run_sequence is not exactly 1..250")
    if _normalize_schedule(scope.get("schedule")) != expected_schedule:
        errors.append("measurement_scope formal schedule drift")

    configuration_hash = _configuration_sha256(config)
    observed_hashes = {str(scope.get("configuration_sha256", ""))}
    observed_hashes.update(str(row.get("configuration_sha256", "")) for row in rows)
    if observed_hashes != {configuration_hash}:
        errors.append(
            "configuration hash mismatch across canonical config, measurement_scope, or raw rows"
        )

    for row in rows:
        run_id = row.get("run_id", "<unknown>")
        try:
            t_form = float(row["data_plane_verified_at"]) - float(row["task_received_at"])
            if t_form != float(row["verified_formation_latency_s"]):
                errors.append(f"{run_id}: T_form does not equal data_plane_verified_at-task_received_at")
        except (KeyError, TypeError, ValueError):
            errors.append(f"{run_id}: malformed T_form timing values")

    raw_run_ids = [str(row.get("run_id", "")) for row in rows]
    expected_run_ids = set(raw_run_ids)
    if "" in expected_run_ids or len(expected_run_ids) != len(raw_run_ids):
        errors.append("formal raw rows do not contain 250 unique non-empty run_id values")

    relevant_event_stages = {
        "TRAFFIC_CONTROL_FINISHED",
        "TASK_RECEIVED",
        "DATA_PLANE_VERIFIED",
        "FORMATION_FAILED",
    }
    events_by_run: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    traffic_control_events = 0
    for event in events:
        run_id = str(event.get("run_id", ""))
        stage = str(event.get("stage", ""))
        if stage in relevant_event_stages:
            if run_id not in expected_run_ids:
                errors.append(f"{stage}: unknown run_id {run_id!r}")
            events_by_run[run_id][stage].append(event)
        if stage == "TRAFFIC_CONTROL_FINISHED":
            traffic_control_events += 1
    if traffic_control_events != len(expected_schedule):
        errors.append(f"formal traffic-control events={traffic_control_events} expected=250")
    for row in rows:
        run_id = row.get("run_id", "")
        by_stage = events_by_run.get(run_id, {})
        traffic_control = by_stage.get("TRAFFIC_CONTROL_FINISHED", [])
        task_received = by_stage.get("TASK_RECEIVED", [])
        data_plane_verified = by_stage.get("DATA_PLANE_VERIFIED", [])
        formation_failed = by_stage.get("FORMATION_FAILED", [])
        if len(traffic_control) != 1:
            errors.append(
                f"{run_id}: expected exactly one TRAFFIC_CONTROL_FINISHED event, found {len(traffic_control)}"
            )
        if len(task_received) != 1:
            errors.append(
                f"{run_id}: expected exactly one TASK_RECEIVED event, found {len(task_received)}"
            )
        success = _bool(row.get("success", ""))
        expected_terminal = data_plane_verified if success else formation_failed
        opposite_terminal = formation_failed if success else data_plane_verified
        expected_stage = "DATA_PLANE_VERIFIED" if success else "FORMATION_FAILED"
        if len(expected_terminal) != 1 or opposite_terminal:
            errors.append(
                f"{run_id}: terminal event inconsistency expected exactly one {expected_stage} and zero opposite events"
            )
        if len(task_received) == 1:
            try:
                if float(task_received[0]["timestamp"]) != float(row["task_received_at"]):
                    errors.append(f"{run_id}: TASK_RECEIVED timestamp does not equal task_received_at")
            except (KeyError, TypeError, ValueError):
                errors.append(f"{run_id}: malformed TASK_RECEIVED timestamp")
        if len(expected_terminal) == 1:
            try:
                if float(expected_terminal[0]["timestamp"]) != float(row["data_plane_verified_at"]):
                    errors.append(
                        f"{run_id}: terminal event timestamp does not equal data_plane_verified_at"
                    )
            except (KeyError, TypeError, ValueError):
                errors.append(f"{run_id}: malformed terminal event timestamp")

    event_result = audit(config, events)
    errors.extend(f"event audit: {error}" for error in event_result["errors"])
    recomputed = _recompute_processed_artifacts(artifact_paths["raw_runs"])
    for name in ("processed_summary", "processed_failures"):
        if artifact_paths[name].read_bytes() != recomputed[name]:
            errors.append(f"{name.replace('_', ' ')} differs from raw-run recomputation")

    artifact_hashes = {name: _sha256_file(path) for name, path in artifact_paths.items()}
    provenance = _capture_versions()
    if manifest_path is not None and not create_manifest:
        if not manifest_path.is_file():
            errors.append(f"manifest missing: {manifest_path}")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("artifact_sha256") != artifact_hashes:
                    errors.append("manifest artifact hash mismatch")
            except json.JSONDecodeError as error:
                errors.append(f"manifest invalid JSON: {error}")

    result = {
        "passed": not errors,
        "errors": errors,
        "formal_runs": len(rows),
        "traffic_control_events": traffic_control_events,
        "event_audit": event_result,
        "artifact_sha256": artifact_hashes,
        "version_provenance": provenance,
    }
    if create_manifest and manifest_path is None:
        errors.append("create-manifest requires a manifest path")
        result["passed"] = False
    if create_manifest and manifest_path is not None and manifest_path.exists():
        errors.append(f"manifest already exists: {manifest_path}")
        result["passed"] = False
    if create_manifest and manifest_path is not None and result["passed"]:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {"artifact_sha256": artifact_hashes, "version_provenance": provenance},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/exp1_netns_verified_formation_v2.yaml")
    parser.add_argument("--events", default="results/exp1_wcnc_final_v2/raw/events.jsonl")
    parser.add_argument(
        "--formal-results-dir",
        help="run strict formal completeness/hash audit for this result directory",
    )
    parser.add_argument("--manifest", help="verify (or create) the formal artifact manifest")
    parser.add_argument(
        "--create-manifest",
        action="store_true",
        help="write a deterministic manifest only after a passing formal audit",
    )
    args = parser.parse_args()
    if args.manifest and not args.formal_results_dir:
        parser.error("--manifest requires --formal-results-dir")
    if args.create_manifest and (not args.formal_results_dir or not args.manifest):
        parser.error("--create-manifest requires --formal-results-dir and --manifest")
    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if args.formal_results_dir:
        result = audit_formal_results(
            Path(args.config),
            Path(args.formal_results_dir),
            manifest_path=Path(args.manifest) if args.manifest else None,
            create_manifest=args.create_manifest,
        )
    else:
        result = audit(config, _read_jsonl(Path(args.events)))
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
