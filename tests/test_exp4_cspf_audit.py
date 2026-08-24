from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_exp4_cspf import audit


FIELDS = (
    "run_id",
    "scenario",
    "seed",
    "method",
    "scenario_fingerprint",
    "fault_fingerprint",
    "fault_effective_at",
    "failure_detected_at",
    "selected_layers",
    "changed_physical_bindings",
    "stable_versions_consistent",
    "staged_rules_empty",
    "residual_rules",
    "recovery_success",
    "qos_recovered",
    "fault_type",
    "fault_was_disruptive",
    "requirement_violated_before_recovery",
    "pre_recovery_requirement_mbps",
    "post_failure_capacity_mbps",
    "pre_recovery_violation_margin_mbps",
    "rule_change_ratio",
    "failure_reason",
)


def row(method: str, seed: int) -> dict[str, object]:
    return {
        "run_id": f"link:seed={seed}:method={method}",
        "scenario": "LINK_FAILURE:post_capacity_to_requirement_0.90",
        "seed": seed,
        "method": method,
        "scenario_fingerprint": "scenario-sha",
        "fault_fingerprint": "fault-sha",
        "fault_effective_at": 1000.0,
        "failure_detected_at": 1000.15,
        "selected_layers": "network" if method == "cspf" else "",
        "changed_physical_bindings": 0,
        "stable_versions_consistent": True,
        "staged_rules_empty": True,
        "residual_rules": 0,
        "recovery_success": True,
        "qos_recovered": True,
        "fault_type": "LINK_DEGRADATION",
        "fault_was_disruptive": True,
        "requirement_violated_before_recovery": True,
        "pre_recovery_requirement_mbps": 10.0,
        "post_failure_capacity_mbps": 9.0,
        "pre_recovery_violation_margin_mbps": 1.0,
        "rule_change_ratio": 0.1,
        "failure_reason": "",
    }


class Exp4CspfAuditTests(unittest.TestCase):
    def write_runs(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def test_audit_rejects_duplicate_seeds_that_hide_a_missing_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runs = root / "raw" / "runs.csv"
            duplicated = [
                row(method, 0)
                for _copy in range(2)
                for method in ("proposed", "full_rebuild", "cspf")
            ]
            self.write_runs(runs, duplicated)

            report = audit(runs, root / "processed", expected_seeds=2)

        self.assertFalse(report["pass"])
        self.assertTrue(report.get("duplicate_run_errors"))
        self.assertTrue(report.get("seed_set_errors"))

    def test_audit_rejects_an_empty_raw_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runs = root / "raw" / "runs.csv"
            self.write_runs(runs, [])

            report = audit(runs, root / "processed", expected_seeds=1)

        self.assertFalse(report["pass"])
        self.assertTrue(report.get("empty_input"))

    def test_audit_rejects_a_cspf_proposal_with_cross_layer_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runs = root / "raw" / "runs.csv"
            self.write_runs(
                runs,
                [row(method, 0) for method in ("proposed", "full_rebuild", "cspf")],
            )
            proposal = {
                "run_id": "link:seed=0:method=cspf",
                "scenario": "LINK_FAILURE:post_capacity_to_requirement_0.90",
                "method": "cspf",
                "selected": True,
                "authorized": True,
                "proposal": {
                    "layer": "physical",
                    "action": "REALLOCATE_RESOURCE",
                    "parameters": {
                        "allowed_inputs": ["topology", "physical_capacity"]
                    },
                },
            }
            (root / "raw" / "proposals.jsonl").write_text(
                json.dumps(proposal) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            report = audit(runs, root / "processed", expected_seeds=1)

        self.assertFalse(report["pass"])
        self.assertTrue(report.get("cspf_proposal_errors"))


if __name__ == "__main__":
    unittest.main()
