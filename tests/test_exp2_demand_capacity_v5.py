from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from experiments import exp2_demand_capacity_ratio as exp2_v5
from experiments.exp2_cross_layer_robustness import run_case
from scripts import aggregate_exp2_demand_capacity as exp2_aggregate
from src.core.cross_layer import (
    ApplicationLayerState,
    CrossLayerTaskState,
    EdgeQoSConstraint,
    LayerProposal,
    NetworkLayerState,
    PhysicalLayerState,
    TransportLayerState,
)
from src.simulation import demand_capacity_truth
from src.simulation.demand_capacity_ratio import DemandCapacityRatioGenerator


EDGE_ID = "edge-truth"
RESOURCE_ID = "shared-truth"


def _state(
    *,
    transport_capacity: float = 8.0,
    network_capacity: float = 12.0,
    physical_capacity: float = 12.0,
    shared_capacity: float = 12.0,
    selected_route: str = "primary",
    access_id: str = "access-primary",
    route_access_requirements: dict[str, dict[str, str]] | None = None,
) -> CrossLayerTaskState:
    return CrossLayerTaskState(
        task_id="task-truth",
        scenario="truth-fixture",
        pressure=1.0,
        application={
            EDGE_ID: ApplicationLayerState(
                task_id="task-truth",
                stage="runtime",
                required_rate_mbps=10.0,
                max_latency_ms=20.0,
                max_loss_rate=0.05,
                min_reliability=0.95,
                priority=2,
                quality_level=2,
            )
        },
        transport={
            EDGE_ID: TransportLayerState(
                session_id="session-truth",
                send_rate_mbps=10.0,
                congestion_window=32.0,
                retransmission_rate=0.001,
                rtt_ms=1.0,
                multipath_enabled=False,
                reliability_mode="balanced",
                reliability=0.999,
                admissible_capacity_mbps=transport_capacity,
            )
        },
        network={
            EDGE_ID: NetworkLayerState(
                route_id=selected_route,
                available_bandwidth_mbps=network_capacity,
                utilization=0.1,
                queue_occupancy=0.0,
                latency_ms=1.0,
                packet_loss_rate=0.001,
                reachable=True,
                candidate_routes=("primary", "reserved"),
                selected_route=selected_route,
                reliability=0.999,
            )
        },
        physical={
            EDGE_ID: PhysicalLayerState(
                access_id=access_id,
                signal_quality=0.97,
                available_capacity_mbps=physical_capacity,
                resource_utilization=0.2,
                reliability=0.999,
                online=True,
                access_latency_ms=1.0,
            )
        },
        constraints={
            EDGE_ID: EdgeQoSConstraint(
                edge_id=EDGE_ID,
                max_latency_ms=20.0,
                max_loss_rate=0.05,
                min_reliability=0.95,
                shared_resource_id=RESOURCE_ID,
                desired_rate_mbps=10.0,
            )
        },
        shared_resource_capacity_mbps={RESOURCE_ID: shared_capacity},
        metadata={
            "stable_version": 2,
            "independent_layer_capacities": True,
            "shared_demand_uses_application_rate": True,
            "shared_resource_background_demand_mbps": {RESOURCE_ID: 0.0},
            "route_access_requirements": route_access_requirements or {},
            "ground_truth_conflict_reference": "keep_combination",
        },
    )


def _proposal(
    layer: str,
    action: str,
    index: int,
    *,
    parameters: dict[str, object] | None = None,
    write_set: frozenset[str] | None = None,
) -> LayerProposal:
    return LayerProposal(
        proposal_id=f"truth:{EDGE_ID}:{layer}:{index}:{action}",
        task_id="task-truth",
        layer=layer,
        action=action,
        target_objects=frozenset({EDGE_ID}),
        read_set=frozenset({f"read:{EDGE_ID}:{layer}"}),
        write_set=write_set or frozenset({f"write:{EDGE_ID}:{layer}"}),
        expected_qos_gain=0.0 if action.startswith("KEEP_") else 0.5,
        expected_cost=0.0 if action.startswith("KEEP_") else 0.2,
        confidence=0.95,
        required_bandwidth_mbps=10.0,
        required_physical_capacity_mbps=10.0,
        expected_latency_ms=3.0,
        expected_loss_rate=0.003,
        affected_edges=frozenset({EDGE_ID}),
        affected_sessions=frozenset({"session-truth"}),
        affected_routes=frozenset({"primary"}),
        affected_gateways=frozenset({"gateway-a", "gateway-b"}),
        parameters=dict(parameters or {}),
    )


def _binary_proposals() -> tuple[LayerProposal, ...]:
    return (
        _proposal(
            "application",
            "KEEP_QUALITY",
            0,
            parameters={"application_rate_mbps": 10.0, "quality_level": 2},
        ),
        _proposal(
            "application",
            "ADAPT_APPLICATION",
            1,
            parameters={"application_rate_mbps": 10.0, "quality_level": 1},
        ),
        _proposal(
            "transport",
            "KEEP_TRANSPORT",
            0,
            parameters={
                "transport_rate_mbps": 10.0,
                "transport_admissible_capacity_mbps": 8.0,
            },
        ),
        _proposal(
            "transport",
            "RESERVE_TRANSPORT_SERVICE",
            1,
            parameters={
                "transport_rate_mbps": 10.0,
                "transport_admissible_capacity_mbps": 12.0,
            },
        ),
        _proposal(
            "network",
            "KEEP_ROUTE",
            0,
            parameters={
                "selected_route": "primary",
                "network_bandwidth_mbps": 12.0,
            },
        ),
        _proposal(
            "network",
            "RESERVE_BANDWIDTH",
            1,
            parameters={
                "route_id": "reserved",
                "selected_route": "reserved",
                "network_bandwidth_mbps": 12.0,
            },
        ),
        _proposal(
            "physical",
            "KEEP_RESOURCE",
            0,
            parameters={
                "access_id": "access-primary",
                "physical_capacity_mbps": 12.0,
            },
        ),
        _proposal(
            "physical",
            "REALLOCATE_RESOURCE",
            1,
            parameters={
                "access_id": "access-alternate",
                "physical_capacity_mbps": 12.0,
            },
        ),
    )


class IndependentTruthEvaluatorTests(unittest.TestCase):
    def test_capacity_failure_is_derived_from_truth_state(self) -> None:
        result = demand_capacity_truth.evaluate_truth(_state())

        self.assertFalse(result.feasible)
        self.assertIn(
            f"application_transport_capacity:{EDGE_ID}", result.violations
        )
        self.assertIn(
            f"transport_admissible_capacity:{EDGE_ID}", result.violations
        )

    def test_selected_transport_reservation_repairs_capacity(self) -> None:
        repair = _proposal(
            "transport",
            "RESERVE_TRANSPORT_SERVICE",
            1,
            parameters={
                "transport_rate_mbps": 10.0,
                "transport_admissible_capacity_mbps": 12.0,
            },
        )

        result = demand_capacity_truth.evaluate_truth(_state(), (repair,))

        self.assertTrue(result.feasible, result.violations)
        self.assertEqual(result.edge_metrics[EDGE_ID].throughput_mbps, 10.0)

    def test_route_access_and_shared_capacity_are_truth_constraints(self) -> None:
        state = _state(
            transport_capacity=12.0,
            shared_capacity=9.0,
            selected_route="reserved",
            access_id="access-primary",
            route_access_requirements={
                EDGE_ID: {"reserved": "access-alternate"}
            },
        )

        result = demand_capacity_truth.evaluate_truth(state)

        self.assertIn(f"network_physical_access:{EDGE_ID}", result.violations)
        self.assertIn(f"shared_resource:{RESOURCE_ID}", result.violations)

    def test_stale_and_overlapping_writes_are_rejected(self) -> None:
        common_write = frozenset({"write:shared"})
        stale_transport = _proposal(
            "transport",
            "RESERVE_TRANSPORT_SERVICE",
            1,
            parameters={
                "observed_version": 1,
                "transport_admissible_capacity_mbps": 12.0,
            },
            write_set=common_write,
        )
        network = _proposal(
            "network",
            "RESERVE_BANDWIDTH",
            1,
            parameters={"network_bandwidth_mbps": 12.0},
            write_set=common_write,
        )

        result = demand_capacity_truth.evaluate_truth(
            _state(), (stale_transport, network)
        )

        self.assertIn(
            f"stale_state:{stale_transport.proposal_id}", result.violations
        )
        self.assertTrue(
            any(value.startswith("write_set:") for value in result.violations),
            result.violations,
        )

    def test_truth_solver_enumerates_binary_four_layer_pool(self) -> None:
        result = demand_capacity_truth.solve_truth(_state(), _binary_proposals())

        self.assertEqual(result.candidate_combinations, 16)
        self.assertTrue(result.ground_truth_conflict)
        self.assertTrue(result.ground_truth_resolvable)
        self.assertEqual(len(result.feasible_combinations), 8)
        self.assertTrue(
            any("RESERVE_TRANSPORT_SERVICE" in value for value in result.best_feasible_combination)
        )

    def test_truth_does_not_call_the_controller_feasibility_predicate(self) -> None:
        with patch(
            "src.controller.feasibility.evaluate_cross_layer_combination",
            side_effect=AssertionError("controller predicate must remain unused"),
        ):
            truth_module = importlib.reload(demand_capacity_truth)
            evaluation = truth_module.evaluate_truth(_state())
            oracle = truth_module.solve_truth(_state(), _binary_proposals())

        self.assertFalse(evaluation.feasible)
        self.assertEqual(oracle.candidate_combinations, 16)


class IndependentPostActivationTests(unittest.TestCase):
    def test_run_case_uses_injected_truth_evaluator_and_rolls_back(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(0.8, 0)
        calls: list[str] = []

        def reject_after_activation(state, proposals=()):
            calls.append(state.task_id)
            result = demand_capacity_truth.evaluate_truth(state, proposals)
            return replace(
                result,
                feasible=False,
                violations=(f"shared_resource:{RESOURCE_ID}",),
            )

        metric, _events, decision = asyncio.run(
            run_case(
                snapshot,
                "proposed",
                run_index=1,
                coordination_timeout_ms=10_000,
                post_evaluator=reject_after_activation,
            )
        )

        self.assertTrue(calls)
        self.assertFalse(metric.qos_satisfied)
        self.assertTrue(metric.rollback_triggered)
        self.assertTrue(metric.rollback_success)
        self.assertIn(
            f"shared_resource:{RESOURCE_ID}",
            decision["post_execution_violations"],
        )

    def test_safe_rejection_event_preserves_run_identity(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(1.4, 9000)

        metric, events, _decision = asyncio.run(
            run_case(
                snapshot,
                "proposed",
                run_index=1,
                coordination_timeout_ms=10_000,
                post_evaluator=demand_capacity_truth.evaluate_truth,
            )
        )

        self.assertTrue(metric.decision_rejected)
        self.assertFalse(metric.transaction_attempted)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["method"], "proposed")
        self.assertEqual(
            events[0]["scenario_fingerprint"], snapshot.fingerprint
        )


class RegisteredV5ProtocolTests(unittest.TestCase):
    def test_v5_generator_preserves_the_reviewed_v4_environment_mapping(
        self,
    ) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(0.8, 0)

        environment = dict(snapshot.metadata["random_environment"])
        environment.pop("distribution_version")
        environment.pop("environment_rng_version")
        digest = hashlib.sha256(
            json.dumps(
                environment,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            DemandCapacityRatioGenerator.distribution_version,
            "wcnc-final-gamma-v5",
        )
        self.assertEqual(
            snapshot.metadata["environment_rng_version"],
            "wcnc-final-gamma-environment-v4",
        )
        self.assertEqual(
            digest,
            "d1205995caa809321d0afd22748cde1b0e7698266fbdbf2b3e4ac2dda85f1604",
        )
        self.assertEqual(snapshot.ground_truth.candidate_combinations, 4096)
        self.assertEqual(
            snapshot.ground_truth.__class__.__module__,
            "src.simulation.demand_capacity_truth",
        )
        self.assertEqual(
            snapshot.metadata["truth_evaluator"],
            "independent-demand-capacity-truth-v1",
        )

    def test_v5_pilot_config_registers_method_free_fresh_output(self) -> None:
        path = Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml")
        config = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(config["experiment"]["phase"], "pilot")
        self.assertTrue(config["experiment"]["frozen"])
        self.assertEqual(
            config["experiment"]["generator_version"],
            "wcnc-final-gamma-v5",
        )
        self.assertEqual(
            config["experiment"]["output_dir"],
            "results/wcnc_pilot_v2/exp2_demand_capacity_ratio_v5",
        )
        self.assertEqual(config["methods"], [])
        exp2_v5.validate_protocol(
            config,
            (),
            tuple(range(9000, 9010)),
            Path(config["experiment"]["output_dir"]),
        )

    def test_v5_formal_config_is_explicitly_unfrozen_before_pilot(self) -> None:
        path = Path("configs/exp2_demand_capacity_ratio_v5.yaml")
        config = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertFalse(config["experiment"]["frozen"])
        self.assertEqual(config["demand_capacity"]["ratios"], [])
        with self.assertRaisesRegex(ValueError, "formal config is not frozen"):
            exp2_v5.validate_protocol(
                config,
                tuple(exp2_v5.METHOD_TO_ENGINE),
                tuple(range(100)),
                Path("results/exp2_demand_capacity_v5"),
            )

    def test_v5_protocol_constants_do_not_reuse_v4_output(self) -> None:
        self.assertEqual(exp2_v5.GENERATOR_VERSION, "wcnc-final-gamma-v5")
        self.assertEqual(
            exp2_v5.FORMAL_OUTPUT,
            Path("results/exp2_demand_capacity_v5"),
        )
        self.assertEqual(
            exp2_v5.PILOT_OUTPUT,
            Path("results/wcnc_pilot_v2/exp2_demand_capacity_ratio_v5"),
        )


class CompleteExecutionProvenanceTests(unittest.TestCase):
    def test_source_manifest_binds_code_commit_and_dependencies(self) -> None:
        manifest = exp2_v5.build_execution_source_manifest(
            require_clean=False
        )

        required = {
            "src/core/cross_layer.py",
            "src/core/events.py",
            "src/core/models.py",
            "src/core/rules.py",
            "src/agents/layer_proposals.py",
            "src/controller/cross_layer_coordinator.py",
            "src/controller/conflicts.py",
            "src/controller/feasibility.py",
            "src/controller/ground_truth.py",
            "src/controller/authorized_actions.py",
            "src/controller/transaction_executor.py",
            "src/metrics/exp2_robustness.py",
            "src/simulation/conflict_robustness.py",
            "src/simulation/conflict_scenario_generator.py",
            "src/simulation/demand_capacity_truth.py",
            "src/simulation/demand_capacity_ratio.py",
            "experiments/exp2_cross_layer_robustness.py",
            "experiments/exp2_demand_capacity_ratio.py",
        }
        self.assertEqual(manifest["schema_version"], "exp2-source-v1")
        self.assertEqual(len(manifest["git_commit"]), 40)
        self.assertTrue(required <= set(manifest["source_sha256"]))
        self.assertEqual(len(manifest["source_tree_sha256"]), 64)
        self.assertEqual(len(manifest["dependencies_sha256"]), 64)
        self.assertTrue(manifest["python"]["version"])
        self.assertTrue(manifest["installed_distributions"])
        exp2_v5.validate_execution_source_manifest(
            manifest,
            require_clean=False,
        )

    def test_source_manifest_rejects_hash_and_cleanliness_tampering(self) -> None:
        manifest = exp2_v5.build_execution_source_manifest(
            require_clean=False
        )
        corrupted = copy.deepcopy(manifest)
        corrupted["source_sha256"][
            "src/simulation/demand_capacity_truth.py"
        ] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source digest"):
            exp2_v5.validate_execution_source_manifest(
                corrupted,
                require_clean=False,
            )

        unclean = copy.deepcopy(manifest)
        unclean["scoped_clean"] = False
        with self.assertRaisesRegex(ValueError, "not clean"):
            exp2_v5.validate_execution_source_manifest(
                unclean,
                require_clean=True,
            )

    def test_persisted_execution_manifest_binds_source_manifest(self) -> None:
        config = yaml.safe_load(
            Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml").read_text(
                encoding="utf-8"
            )
        )
        source_manifest = exp2_v5.build_execution_source_manifest(
            require_clean=False
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            raw_dir = Path(temporary_directory) / "raw"
            manifest, manifest_sha = exp2_v5.persist_execution_provenance(
                raw_dir,
                config=config,
                methods=(),
                seeds=tuple(range(9000, 9010)),
                ratios=tuple(config["demand_capacity"]["ratios"]),
                output_dir=Path(config["experiment"]["output_dir"]),
                source_manifest=source_manifest,
                require_clean_source=False,
            )

            persisted_source = json.loads(
                (raw_dir / "execution_source_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            persisted_execution = json.loads(
                (raw_dir / "execution_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        expected_source_sha = exp2_v5.sha256_json(source_manifest)
        self.assertEqual(persisted_source, source_manifest)
        self.assertEqual(
            manifest["source_manifest_sha256"], expected_source_sha
        )
        self.assertEqual(persisted_execution, manifest)
        self.assertEqual(exp2_v5.sha256_json(manifest), manifest_sha)

    def test_pilot_checks_clean_source_before_generating_scenarios(self) -> None:
        config = yaml.safe_load(
            Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml").read_text(
                encoding="utf-8"
            )
        )
        with patch.object(
            exp2_v5,
            "build_execution_source_manifest",
            side_effect=ValueError("source dirty"),
        ), patch.object(
            exp2_v5.DemandCapacityRatioGenerator,
            "generate",
            side_effect=AssertionError("generation happened before provenance"),
        ) as generate:
            with self.assertRaisesRegex(ValueError, "source dirty"):
                exp2_v5.run_ground_truth_pilot(
                    config,
                    seeds=tuple(range(9000, 9010)),
                    output_dir=Path(
                        "results/wcnc_pilot_v2/exp2_demand_capacity_ratio_v5"
                    ),
                )
        generate.assert_not_called()

    def test_formal_binding_rejects_tampered_pilot_execution_manifest(self) -> None:
        pilot_config = yaml.safe_load(
            Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml").read_text(
                encoding="utf-8"
            )
        )
        source_manifest = exp2_v5.build_execution_source_manifest(
            require_clean=False
        )
        source_manifest["scoped_clean"] = True
        execution_manifest = {
            "phase": "pilot",
            "generator_version": "wcnc-final-gamma-v5",
            "configuration_sha256": exp2_v5.sha256_json(pilot_config),
            "methods": [],
            "seeds": list(range(9000, 9010)),
            "ratios": list(exp2_v5.PILOT_GAMMA_GRID),
            "output_dir": str(exp2_v5.PILOT_OUTPUT),
            "source_manifest_sha256": exp2_v5.sha256_json(source_manifest),
        }
        selection = {
            "generator_version": "wcnc-final-gamma-v5",
            "selection_inputs": "ground_truth_class_only",
            "configuration_sha256": exp2_v5.sha256_json(pilot_config),
            "retained_ratios": [0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3],
            "execution_manifest": execution_manifest,
            "execution_manifest_sha256": exp2_v5.sha256_json(execution_manifest),
            "source_manifest_sha256": exp2_v5.sha256_json(source_manifest),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pilot_config_path = root / "pilot.yaml"
            pilot_config_path.write_text(
                yaml.safe_dump(pilot_config, sort_keys=False), encoding="utf-8"
            )
            selection_path = root / "pilot_selection.json"
            selection_path.write_text(
                json.dumps(selection, sort_keys=True) + "\n", encoding="utf-8"
            )
            execution_path = root / "execution_manifest.json"
            execution_path.write_text(
                json.dumps(execution_manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            source_path = root / "execution_source_manifest.json"
            source_path.write_text(
                json.dumps(source_manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            generator_path = Path("src/simulation/demand_capacity_ratio.py")
            truth_path = Path("src/simulation/demand_capacity_truth.py")

            formal = yaml.safe_load(
                Path("configs/exp2_demand_capacity_ratio_v5.yaml").read_text(
                    encoding="utf-8"
                )
            )
            formal["experiment"].update(
                {
                    "frozen": True,
                    "pilot_selection_path": str(selection_path),
                    "pilot_selection_sha256": exp2_v5.sha256_file(selection_path),
                    "pilot_config_path": str(pilot_config_path),
                    "pilot_config_sha256": exp2_v5.sha256_file(pilot_config_path),
                    "pilot_execution_manifest_path": str(execution_path),
                    "pilot_execution_manifest_sha256": exp2_v5.sha256_file(
                        execution_path
                    ),
                    "pilot_source_manifest_path": str(source_path),
                    "pilot_source_manifest_sha256": exp2_v5.sha256_file(source_path),
                    "generator_source_path": str(generator_path),
                    "generator_source_sha256": exp2_v5.sha256_file(generator_path),
                    "truth_source_path": str(truth_path),
                    "truth_source_sha256": exp2_v5.sha256_file(truth_path),
                    "critical_source_tree_sha256": source_manifest[
                        "source_tree_sha256"
                    ],
                }
            )
            formal["demand_capacity"]["ratios"] = selection[
                "retained_ratios"
            ]
            exp2_v5.validate_formal_pilot_binding(formal)

            execution_manifest["methods"] = ["ours"]
            execution_path.write_text(
                json.dumps(execution_manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "pilot execution manifest"):
                exp2_v5.validate_formal_pilot_binding(formal)


class V5AggregationAuditTests(unittest.TestCase):
    def test_v5_provenance_audit_revalidates_full_formal_binding(self) -> None:
        config = yaml.safe_load(
            Path("configs/exp2_demand_capacity_ratio_v5.yaml").read_text(
                encoding="utf-8"
            )
        )
        manifest = {
            "phase": "formal",
            "generator_version": "wcnc-final-gamma-v5",
            "configuration_sha256": exp2_v5.sha256_json(config),
            "methods": [],
            "seeds": [],
            "ratios": [],
            "output_dir": "results/exp2_demand_capacity_v5",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            raw_dir = Path(temporary_directory)
            (raw_dir / "configuration.json").write_text(
                json.dumps(config) + "\n", encoding="utf-8"
            )
            (raw_dir / "execution_manifest.json").write_text(
                json.dumps(manifest) + "\n", encoding="utf-8"
            )
            errors = exp2_aggregate._audit_provenance_files(
                raw_dir,
                [],
                ratios=(),
                seeds=(),
                methods=(),
            )

        self.assertTrue(
            any(value.startswith("formal_pilot_binding:") for value in errors),
            errors,
        )

    def test_source_manifest_audit_binds_execution_manifest(self) -> None:
        source = exp2_v5.build_execution_source_manifest(require_clean=False)
        execution = {
            "source_manifest_sha256": exp2_v5.sha256_json(source)
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            raw_dir = Path(temporary_directory)
            (raw_dir / "execution_source_manifest.json").write_text(
                json.dumps(source) + "\n", encoding="utf-8"
            )
            (raw_dir / "execution_manifest.json").write_text(
                json.dumps(execution) + "\n", encoding="utf-8"
            )
            self.assertEqual(
                exp2_aggregate.audit_v5_source_manifest(
                    raw_dir, require_clean=False
                ),
                [],
            )

            execution["source_manifest_sha256"] = "0" * 64
            (raw_dir / "execution_manifest.json").write_text(
                json.dumps(execution) + "\n", encoding="utf-8"
            )
            errors = exp2_aggregate.audit_v5_source_manifest(
                raw_dir, require_clean=False
            )
        self.assertTrue(any("source_manifest_sha256" in value for value in errors))

    def test_run_decision_event_link_audit_detects_semantic_drift(self) -> None:
        run = {
            "run_id": "gamma=1.2:seed=0:method=ours",
            "method": "ours",
            "scenario_fingerprint": "a" * 64,
            "qos_satisfied": "True",
            "transaction_attempted": "True",
            "rollback_triggered": "False",
            "rollback_success": "False",
            "safe_rejection": "False",
        }
        decision = {
            **run,
            "qos_satisfied": True,
            "transaction_attempted": True,
            "rollback_triggered": False,
            "rollback_success": False,
            "safe_rejection": False,
        }
        event = {
            "experiment_run_id": run["run_id"],
            "method": "ours",
            "scenario_fingerprint": run["scenario_fingerprint"],
            "event_stage": "ACTIVATED",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            decisions_path = root / "decisions.jsonl"
            events_path = root / "events.jsonl"
            decisions_path.write_text(
                json.dumps(decision) + "\n", encoding="utf-8"
            )
            events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            self.assertEqual(
                exp2_aggregate.audit_run_decision_event_links(
                    [run], decisions_path, events_path
                ),
                [],
            )

            decision["qos_satisfied"] = False
            decisions_path.write_text(
                json.dumps(decision) + "\n", encoding="utf-8"
            )
            errors = exp2_aggregate.audit_run_decision_event_links(
                [run], decisions_path, events_path
            )
        self.assertTrue(
            any("qos_satisfied" in value for value in errors), errors
        )

    def test_independent_oracle_replay_detects_truth_corruption(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(1.2, 0)
        scenario = exp2_v5.compact_scenario_row(snapshot)
        config = yaml.safe_load(
            Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            scenarios_path = Path(temporary_directory) / "scenarios.jsonl"
            scenarios_path.write_text(
                json.dumps(scenario) + "\n", encoding="utf-8"
            )
            self.assertEqual(
                exp2_aggregate.replay_v5_oracle(scenarios_path, config), []
            )

            scenario["ground_truth"]["feasible_combinations"] += 1
            scenarios_path.write_text(
                json.dumps(scenario) + "\n", encoding="utf-8"
            )
            errors = exp2_aggregate.replay_v5_oracle(
                scenarios_path, config
            )
        self.assertTrue(any("feasible_combinations" in value for value in errors))

    def test_conditional_summary_uses_only_oracle_feasible_scenarios(self) -> None:
        rows = [
            {
                "demand_to_capacity_ratio": "1.2",
                "method": "ours",
                "qos_satisfied": "True",
                "ground_truth_feasible_combinations": "10",
            },
            {
                "demand_to_capacity_ratio": "1.2",
                "method": "ours",
                "qos_satisfied": "False",
                "ground_truth_feasible_combinations": "2",
            },
            {
                "demand_to_capacity_ratio": "1.2",
                "method": "ours",
                "qos_satisfied": "False",
                "ground_truth_feasible_combinations": "0",
            },
        ]

        summary = exp2_aggregate.conditional_feasible_summary(rows)

        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["oracle_feasible_samples"], 2)
        self.assertEqual(summary[0]["successful_samples"], 1)
        self.assertEqual(
            summary[0]["conditional_task_satisfaction_rate_mean"], 0.5
        )


if __name__ == "__main__":
    unittest.main()
