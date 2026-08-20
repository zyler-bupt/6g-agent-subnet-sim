from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.exp1_initial_formation import run_exp1
from experiments.exp2_conflict import run_exp2
from experiments.exp3_business_elasticity import run_exp3
from experiments.exp4_failure import run_exp4
from experiments.paper_protocol import EXPERIMENT_METHODS
from scripts.aggregate_results import aggregate_experiment
from scripts.plot_final_paper_figures import plot_exp1, plot_exp2, plot_exp3, plot_exp4
from scripts.sanity_check_results import check_results


PAPER_ORDER = ("exp1", "exp3", "exp4", "exp2")
_PLOTTERS = {
    "exp1": plot_exp1,
    "exp2": plot_exp2,
    "exp3": plot_exp3,
    "exp4": plot_exp4,
}
_FIGURE_STEMS = {
    "exp1": "Fig1_Formation",
    "exp2": "Fig2_Cross_Layer_Coordination",
    "exp3": "Fig3_Business_Elasticity",
    "exp4": "Fig4_Failure_Recovery",
}


@dataclass(frozen=True)
class PaperRunResult:
    experiments: tuple[str, ...]
    raw_trial_count: int
    aggregate_row_count: int
    error_count: int
    warning_count: int
    output_root: Path


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_pilot_manifest(
    output_root: Path,
    experiment: str,
    *,
    config_path: Path = Path("configs/paper_experiments.yaml"),
) -> Path:
    raw_path = output_root / "raw" / "pilot" / experiment / "trials.csv"
    aggregate_path = output_root / "aggregated" / "pilot" / experiment / "summary.csv"
    sanity_path = output_root / "aggregated" / "pilot" / experiment / "sanity.json"
    if not all(path.exists() for path in (raw_path, aggregate_path, sanity_path)):
        raise RuntimeError(f"cannot manifest incomplete {experiment} pilot")
    rows = _read_csv(raw_path)
    sanity = json.loads(sanity_path.read_text(encoding="utf-8"))
    unique_trials = {row["trial_id"] for row in rows}
    manifest = {
        "experiment": experiment,
        "mode": "pilot",
        "config_path": str(config_path),
        "config_sha256": sha256_file(config_path),
        "raw_path": str(raw_path.relative_to(output_root)),
        "raw_sha256": sha256_file(raw_path),
        "aggregate_path": str(aggregate_path.relative_to(output_root)),
        "aggregate_sha256": sha256_file(aggregate_path),
        "sanity_path": str(sanity_path.relative_to(output_root)),
        "sanity_sha256": sha256_file(sanity_path),
        "methods": sorted({row["method_id"] for row in rows}),
        "seeds": sorted({int(row["seed"]) for row in rows}),
        "event_ids": sorted({int(row["event_id"]) for row in rows}),
        "raw_trial_count": len(rows),
        "paired_instance_count": len(unique_trials),
        "error_count": sum(item.get("level") == "ERROR" for item in sanity),
        "warning_count": sum(item.get("level") == "WARNING" for item in sanity),
        "warning_explanations": [
            item.get("message", "")
            for item in sanity
            if item.get("level") == "WARNING"
        ],
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    destination = output_root / "aggregated" / "pilot" / experiment / "pilot_manifest.json"
    _write_json(destination, manifest)
    return destination


def validate_pilot_manifest(
    manifest: Mapping[str, Any],
    experiment: str,
    *,
    current_config_hash: str,
) -> None:
    if manifest.get("experiment") != experiment or manifest.get("mode") != "pilot":
        raise RuntimeError(f"pilot gate failed for {experiment}: identity mismatch")
    actual_methods = set(str(item) for item in manifest.get("methods", ()))
    expected_methods = set(EXPERIMENT_METHODS[experiment])
    if actual_methods != expected_methods:
        raise RuntimeError(
            f"pilot gate failed for {experiment}: method set {sorted(actual_methods)} "
            f"!= {sorted(expected_methods)}"
        )
    if manifest.get("config_sha256") != current_config_hash:
        raise RuntimeError(f"pilot gate failed for {experiment}: config hash mismatch")
    if int(manifest.get("error_count", -1)) != 0:
        raise RuntimeError(f"pilot gate failed for {experiment}: sanity errors exist")
    for field in ("raw_sha256", "aggregate_sha256", "sanity_sha256"):
        if not manifest.get(field):
            raise RuntimeError(f"pilot gate failed for {experiment}: missing {field}")


def validate_all_pilots(
    output_root: Path,
    *,
    config_path: Path = Path("configs/paper_experiments.yaml"),
) -> dict[str, dict[str, Any]]:
    current_config_hash = sha256_file(config_path)
    manifests: dict[str, dict[str, Any]] = {}
    for experiment in ("exp1", "exp2", "exp3", "exp4"):
        path = output_root / "aggregated" / "pilot" / experiment / "pilot_manifest.json"
        if not path.exists():
            raise RuntimeError(f"pilot gate failed: missing {experiment} manifest")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        validate_pilot_manifest(
            manifest,
            experiment,
            current_config_hash=current_config_hash,
        )
        for path_field, hash_field in (
            ("raw_path", "raw_sha256"),
            ("aggregate_path", "aggregate_sha256"),
            ("sanity_path", "sanity_sha256"),
        ):
            artifact = output_root / str(manifest.get(path_field, ""))
            if not artifact.is_file() or sha256_file(artifact) != manifest[hash_field]:
                raise RuntimeError(
                    f"pilot gate failed for {experiment}: {path_field} hash mismatch"
                )
        manifests[experiment] = manifest
    return manifests


async def run_paper(
    experiments: Sequence[str] = PAPER_ORDER,
    *,
    output_root: Path = Path("results"),
    config_path: Path = Path("configs/paper_experiments.yaml"),
    bootstrap_iterations: int = 5000,
    replace_existing: bool = False,
) -> PaperRunResult:
    selected = tuple(dict.fromkeys(experiments))
    unsupported = set(selected) - set(PAPER_ORDER)
    if unsupported or not selected:
        raise ValueError("unsupported or empty paper experiment selection")
    validate_all_pilots(output_root, config_path=config_path)

    total_raw = 0
    total_aggregates = 0
    total_errors = 0
    total_warnings = 0
    for experiment in selected:
        raw_path = output_root / "raw" / "paper" / experiment / "trials.csv"
        aggregate_dir = output_root / "aggregated" / "paper" / experiment
        summary_path = aggregate_dir / "summary.csv"
        if raw_path.exists() and not replace_existing:
            raise FileExistsError(
                f"paper output already exists at {raw_path}; use --replace-paper"
            )
        if replace_existing:
            _clear_paper_experiment(output_root, experiment)
        print(f"[paper] starting {experiment}", flush=True)
        runner = {
            "exp1": run_exp1,
            "exp2": run_exp2,
            "exp3": run_exp3,
            "exp4": run_exp4,
        }[experiment]
        rows = await runner("paper", output_root)
        aggregates = aggregate_experiment(
            raw_path,
            summary_path,
            bootstrap_iterations=bootstrap_iterations,
        )
        findings = check_results(raw_path, experiment=experiment)
        aggregate_dir.mkdir(parents=True, exist_ok=True)
        sanity_path = aggregate_dir / "sanity.json"
        _write_json(sanity_path, [asdict(item) for item in findings])
        errors = sum(item.level == "ERROR" for item in findings)
        warnings = sum(item.level == "WARNING" for item in findings)
        if errors:
            raise RuntimeError(
                f"paper sanity failed for {experiment} with {errors} error(s)"
            )
        _PLOTTERS[experiment](summary_path, output_root / "paper_figures")
        manifest = {
            "experiment": experiment,
            "mode": "paper",
            "config_sha256": sha256_file(config_path),
            "raw_sha256": sha256_file(raw_path),
            "aggregate_sha256": sha256_file(summary_path),
            "sanity_sha256": sha256_file(sanity_path),
            "methods": list(EXPERIMENT_METHODS[experiment]),
            "raw_trial_count": len(rows),
            "paired_instance_count": len({row.trial_id for row in rows}),
            "topology_seed_count": len({row.seed for row in rows}),
            "error_count": errors,
            "warning_count": warnings,
            "warning_explanations": [
                item.message for item in findings if item.level == "WARNING"
            ],
        }
        _write_json(aggregate_dir / "paper_manifest.json", manifest)
        total_raw += len(rows)
        total_aggregates += len(aggregates)
        total_errors += errors
        total_warnings += warnings
        print(
            f"[paper] completed {experiment}: raw={len(rows)}, "
            f"aggregate={len(aggregates)}, errors={errors}, warnings={warnings}",
            flush=True,
        )
    return PaperRunResult(
        experiments=selected,
        raw_trial_count=total_raw,
        aggregate_row_count=total_aggregates,
        error_count=total_errors,
        warning_count=total_warnings,
        output_root=output_root,
    )


def _clear_paper_experiment(output_root: Path, experiment: str) -> None:
    for directory in (
        output_root / "raw" / "paper" / experiment,
        output_root / "aggregated" / "paper" / experiment,
    ):
        if directory.exists():
            shutil.rmtree(directory)
    stem = _FIGURE_STEMS[experiment]
    for suffix in ("pdf", "png"):
        figure = output_root / "paper_figures" / f"{stem}.{suffix}"
        if figure.exists():
            figure.unlink()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run gated formal paper experiments")
    parser.add_argument("--experiments", nargs="+", default=PAPER_ORDER)
    parser.add_argument("--output-root", type=Path, default=Path("results"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/paper_experiments.yaml"),
    )
    parser.add_argument("--bootstrap-iterations", type=int, default=5000)
    parser.add_argument("--replace-paper", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(
        run_paper(
            args.experiments,
            output_root=args.output_root,
            config_path=args.config,
            bootstrap_iterations=args.bootstrap_iterations,
            replace_existing=args.replace_paper,
        )
    )
    print(
        f"paper wrote {result.raw_trial_count} raw trials and "
        f"{result.aggregate_row_count} aggregate rows "
        f"(errors={result.error_count}, warnings={result.warning_count})"
    )


if __name__ == "__main__":
    main()
