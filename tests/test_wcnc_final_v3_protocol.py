from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

import yaml

from experiments.paper_protocol import (
    EXPERIMENT_FAILURE_METHODS,
    EXPERIMENT_METHODS,
    METHODS,
    PROTOCOL_ID,
    load_and_validate_wcnc_v3_config,
)
from src.metrics.paper import PaperTrial


ROOT = Path(__file__).resolve().parents[1]


class WcncFinalV3ProtocolTests(unittest.TestCase):
    def test_protocol_id_method_sets_and_labels_are_frozen(self) -> None:
        self.assertEqual(PROTOCOL_ID, "wcnc_final_v3")
        self.assertEqual(
            EXPERIMENT_METHODS["exp1"],
            ("proposed", "cspf", "global_sfc_embedding"),
        )
        self.assertEqual(
            EXPERIMENT_METHODS["exp2"],
            ("proposed", "sanet_dw", "weighted_sum", "independent"),
        )
        self.assertEqual(
            EXPERIMENT_METHODS["exp3"],
            ("proposed", "netren", "local_only", "full_rebuild"),
        )
        self.assertEqual(
            EXPERIMENT_FAILURE_METHODS["exp4"]["link_failure"],
            ("proposed", "cspf", "full_rebuild"),
        )
        self.assertEqual(METHODS["proposed"].label, "Ours")
        self.assertEqual(METHODS["cspf"].experiment_labels["exp1"], "CSPF-based Formation")
        self.assertEqual(METHODS["cspf"].experiment_labels["exp4"], "CSPF Recovery")
        self.assertEqual(METHODS["global_sfc_embedding"].label, "Global SFC Embedding (Heuristic)")
        self.assertEqual(METHODS["sanet_dw"].label, "SANet-DW*")
        self.assertEqual(METHODS["netren"].label, "NetRen*")
        self.assertTrue(METHODS["sanet_dw"].adapted)
        self.assertTrue(METHODS["netren"].adapted)

    def test_frozen_config_contains_exact_grids_and_seed_ranges(self) -> None:
        path = ROOT / "configs" / "wcnc_final_v3.yaml"
        config = load_and_validate_wcnc_v3_config(path)
        self.assertEqual(config["protocol"]["id"], PROTOCOL_ID)
        self.assertEqual(config["pilot"]["seeds"], "9000:9019")
        self.assertEqual(config["formal"]["exp1_seeds"], "0:49")
        self.assertEqual(config["formal"]["exp2_exp4_seeds"], "0:99")
        self.assertEqual(config["exp1"]["num_agents"], [4, 8, 12, 16, 20])
        self.assertEqual(config["exp2"]["gamma"], [0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3])
        self.assertEqual(config["exp3"]["affected_dependency_scope_percent"], [10, 20, 30, 40, 50])
        self.assertEqual(config["exp4"]["capacity_ratio"], [1.1, 1.0, 0.9, 0.75, 0.6])

        drifted = copy.deepcopy(config)
        drifted["exp2"]["gamma"] = [0.8, 1.0, 1.3]
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "wcnc_final_v3.yaml"
            candidate.write_text(yaml.safe_dump(drifted), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "protocol drift"):
                load_and_validate_wcnc_v3_config(candidate)

    def test_canonical_trial_schema_separates_online_and_verifier_metrics(self) -> None:
        names = {field.name for field in fields(PaperTrial)}
        required = {
            "protocol_id",
            "observation_fingerprint",
            "oracle_fingerprint",
            "true_state_schema_version",
            "observed_state_schema_version",
            "pre_verification_decision_correct",
            "pre_verification_feasible",
            "unsafe_proposal_before_verification",
            "verification_rescued",
            "search_timeout",
            "evaluated_combinations",
            "applicable",
            "timeout",
        }
        self.assertEqual(required - names, set())


if __name__ == "__main__":
    unittest.main()
