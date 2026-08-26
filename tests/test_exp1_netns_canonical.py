from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

import experiments.exp1_netns_verified_formation as exp1


class Exp1NamespaceIsolationTests(unittest.TestCase):
    def test_interface_enumeration_uses_current_netns_netlink_view(self) -> None:
        ip_result = SimpleNamespace(
            stdout='[{"ifname":"lo"}]',
        )
        host_sysfs_entries = [Path("/sys/class/net/lo"), Path("/sys/class/net/docker0")]
        with (
            patch.object(exp1.subprocess, "run", return_value=ip_result) as run,
            patch.object(Path, "iterdir", return_value=host_sysfs_entries),
        ):
            self.assertEqual(exp1._network_interface_names(), {"lo"})
        run.assert_called_once_with(
            ("ip", "-j", "link", "show"),
            check=True,
            text=True,
            capture_output=True,
        )

    def test_manual_sentinel_without_parent_namespace_provenance_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "provenance"):
            exp1.validate_isolated_outer_namespace(
                {"WCNC_EXP1_INSIDE_USERNS": "1"}, current_inode=200
            )

    def test_sentinel_in_the_parent_namespace_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not isolated"):
            exp1.validate_isolated_outer_namespace(
                {
                    "WCNC_EXP1_INSIDE_USERNS": "1",
                    "WCNC_EXP1_PARENT_NETNS_INODE": "200",
                },
                current_inode=200,
            )

    def test_child_namespace_with_matching_provenance_is_accepted(self) -> None:
        exp1.validate_isolated_outer_namespace(
            {
                "WCNC_EXP1_INSIDE_USERNS": "1",
                "WCNC_EXP1_PARENT_NETNS_INODE": "200",
            },
            current_inode=201,
            init_inode=200,
            interface_names={"lo"},
        )

    def test_pid_one_inode_access_errors_do_not_override_other_evidence(self) -> None:
        for error_type in (PermissionError, FileNotFoundError):
            with self.subTest(error_type=error_type.__name__), patch.object(
                exp1,
                "_init_network_namespace_inode",
                side_effect=error_type("unavailable PID 1 netns"),
            ):
                try:
                    exp1.validate_isolated_outer_namespace(
                        {
                            "WCNC_EXP1_INSIDE_USERNS": "1",
                            "WCNC_EXP1_PARENT_NETNS_INODE": "200",
                        },
                        current_inode=201,
                        interface_names={"lo"},
                    )
                except (PermissionError, FileNotFoundError) as error:
                    self.fail(f"optional PID 1 check leaked access error: {error}")

    def test_non_loopback_netlink_interface_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "expected only lo"):
            exp1.validate_isolated_outer_namespace(
                {
                    "WCNC_EXP1_INSIDE_USERNS": "1",
                    "WCNC_EXP1_PARENT_NETNS_INODE": "200",
                },
                current_inode=201,
                init_inode=200,
                interface_names={"lo", "eth0"},
            )

    def test_forged_parent_inode_cannot_authorize_the_host_namespace(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PID 1"):
            exp1.validate_isolated_outer_namespace(
                {
                    "WCNC_EXP1_INSIDE_USERNS": "1",
                    "WCNC_EXP1_PARENT_NETNS_INODE": "0",
                },
                current_inode=200,
                init_inode=200,
                interface_names={"lo", "eth0"},
            )

    def test_root_and_unprivileged_reexec_use_the_correct_unshare_modes(self) -> None:
        root = exp1.namespace_reexec_command(("--seeds", "0:49"), euid=0, python="python")
        user = exp1.namespace_reexec_command(("--seeds", "0:49"), euid=1000, python="python")
        self.assertEqual(
            root,
            ("unshare", "--net", "--fork", "python", "-m", "experiments.exp1_netns_verified_formation", "--seeds", "0:49"),
        )
        self.assertEqual(
            user,
            ("unshare", "--user", "--map-root-user", "--net", "--fork", "python", "-m", "experiments.exp1_netns_verified_formation", "--seeds", "0:49"),
        )

    def test_internal_reexec_restores_sentinel_and_parent_provenance(self) -> None:
        completed = SimpleNamespace(returncode=0)
        with (
            patch.dict(exp1.os.environ, {}, clear=True),
            patch.object(exp1, "_network_namespace_inode", return_value=321),
            patch.object(exp1.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(exp1._reexec_in_user_namespace(("--seeds", "0:1")), 0)
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["WCNC_EXP1_INSIDE_USERNS"], "1")
        self.assertEqual(environment["WCNC_EXP1_PARENT_NETNS_INODE"], "321")


class Exp1DeploymentPlanTests(unittest.TestCase):
    @staticmethod
    def topology() -> exp1.ProcessNetnsTopology:
        topology = exp1.ProcessNetnsTopology(3, 2)
        topology.agents = [SimpleNamespace(pid=100 + index) for index in range(3)]
        topology.gateways = [SimpleNamespace(pid=200 + index) for index in range(2)]
        topology.agent_ips = ["10.100.0.2", "10.101.1.2", "10.100.2.2"]
        topology.agent_gateways = [0, 1, 0]
        return topology

    @staticmethod
    def edges() -> tuple[exp1.FormationEdge, ...]:
        return (
            exp1.FormationEdge("edge-0", 0, 1, 0.5),
            exp1.FormationEdge("edge-1", 1, 2, 0.5),
        )

    def test_methods_produce_distinct_executable_command_plans(self) -> None:
        topology = self.topology()
        plans = {
            method: topology.plan_task_routes(method, self.edges())
            for method in ("proposed", "cspf", "global_sfc_embedding")
        }
        signatures = {
            method: tuple(
                tuple((command.namespace, command.argv) for command in batch.commands)
                for batch in plan.batches
            )
            for method, plan in plans.items()
        }
        self.assertEqual(len(set(signatures.values())), 3)
        self.assertEqual(plans["proposed"].control_messages, 1)
        self.assertEqual(plans["cspf"].control_messages, 3)
        self.assertEqual(plans["global_sfc_embedding"].control_messages, 6)
        self.assertEqual(plans["proposed"].rules_installed, plans["cspf"].rules_installed)
        self.assertLess(plans["cspf"].rules_installed, plans["global_sfc_embedding"].rules_installed)

    def test_global_sfc_policy_tables_can_resolve_each_local_gateway(self) -> None:
        plan = self.topology().plan_task_routes(
            "global_sfc_embedding", self.edges()
        )
        commands = {
            command.argv
            for batch in plan.batches
            for command in batch.commands
        }
        self.assertIn(
            (
                "ip", "route", "replace", "table", "100",
                "10.100.0.0/30", "dev", "eth0", "scope", "link",
            ),
            commands,
        )
        self.assertIn(
            (
                "ip", "route", "replace", "table", "101",
                "10.101.1.0/30", "dev", "eth0", "scope", "link",
            ),
            commands,
        )
        self.assertIn(
            (
                "ip", "route", "replace", "table", "102",
                "10.100.2.0/30", "dev", "eth0", "scope", "link",
            ),
            commands,
        )

    def test_global_sfc_activates_each_hop_by_destination_before_source_selection(self) -> None:
        plan = self.topology().plan_task_routes(
            "global_sfc_embedding", self.edges()
        )
        rule_commands = [
            command.argv
            for batch in plan.batches
            for command in batch.commands
            if command.argv[:2] == ("ip", "rule")
        ]
        self.assertTrue(rule_commands)
        self.assertTrue(all("to" in command for command in rule_commands))
        self.assertTrue(all("from" not in command for command in rule_commands))
        self.assertIn(
            (
                "ip", "rule", "add", "priority", "10001",
                "to", "10.101.1.2/32", "lookup", "100",
            ),
            rule_commands,
        )

    def test_cspf_prunes_a_path_when_an_observed_link_lacks_bandwidth(self) -> None:
        topology = self.topology()
        profiles = tuple(
            exp1.TrafficControlProfile(
                endpoint=endpoint,
                namespace_pid=None,
                interface="test0",
                base_delay_ms=1.0,
                jitter_ms=0.0,
                packet_loss_percent=0.0,
                bandwidth_mbps=0.1 if endpoint == "agent-0:eth0" else 10.0,
                queue_limit_packets=100,
            )
            for endpoint in (
                "agent-0:eth0", "gateway-0:ga0", "gateway-0:up0", "outer:og0",
                "outer:og1", "gateway-1:up0", "gateway-1:ga1", "agent-1:eth0",
                "agent-2:eth0", "gateway-0:ga2",
            )
        )
        with self.assertRaisesRegex(RuntimeError, "bandwidth"):
            topology.plan_task_routes("cspf", self.edges(), profiles=profiles)

    def test_cspf_prunes_a_path_that_exceeds_the_observed_delay_constraint(self) -> None:
        topology = self.topology()
        profiles = tuple(
            exp1.TrafficControlProfile(
                endpoint=endpoint,
                namespace_pid=None,
                interface="test0",
                base_delay_ms=50.0,
                jitter_ms=0.0,
                packet_loss_percent=0.0,
                bandwidth_mbps=10.0,
                queue_limit_packets=100,
            )
            for endpoint in (
                "agent-0:eth0", "gateway-0:ga0", "gateway-0:up0", "outer:og0",
                "outer:og1", "gateway-1:up0", "gateway-1:ga1", "agent-1:eth0",
                "agent-2:eth0", "gateway-0:ga2",
            )
        )
        with self.assertRaisesRegex(RuntimeError, "delay"):
            topology.plan_task_routes(
                "cspf", self.edges(), profiles=profiles, max_path_delay_ms=300.0
            )

    def test_plan_execution_preserves_sequential_batches_and_parallel_commands(self) -> None:
        topology = self.topology()
        plan = topology.plan_task_routes("cspf", self.edges())
        executed: list[tuple[tuple[int | None, tuple[str, ...]], ...]] = []
        topology._parallel_commands = lambda commands, *, check: executed.append(tuple(commands))
        control_messages, rules_installed = topology.install_deployment_plan(plan)
        self.assertEqual(len(executed), 3)
        self.assertEqual(control_messages, 3)
        self.assertEqual(rules_installed, 9)
        self.assertEqual(len(topology.last_install_commands), 9)


class Exp1FailureContractTests(unittest.TestCase):
    def test_method_order_rotates_by_paired_seed(self) -> None:
        methods = ("proposed", "cspf", "global_sfc_embedding")
        self.assertEqual(exp1.counterbalanced_method_order(methods, 0), methods)
        self.assertEqual(
            exp1.counterbalanced_method_order(methods, 1),
            ("cspf", "global_sfc_embedding", "proposed"),
        )
        self.assertEqual(
            exp1.counterbalanced_method_order(methods, 2),
            ("global_sfc_embedding", "proposed", "cspf"),
        )
    def test_v3_formal_config_freezes_method_owned_deployment_policies(self) -> None:
        with Path("configs/exp1_netns_verified_formation_v3.yaml").open(
            encoding="utf-8"
        ) as handle:
            config = yaml.safe_load(handle)
        exp1.validate_formal_protocol(config, tuple(range(50)))
        config["deployment"]["proposed"]["scheduling"] = "sequential"
        with self.assertRaisesRegex(ValueError, "deployment.proposed.scheduling"):
            exp1.validate_formal_protocol(config, tuple(range(50)))

    def test_nonformal_smoke_accepts_two_seeds_and_one_task_size(self) -> None:
        config = {
            "experiment": {"phase": "pilot"},
            "simulation": {"task_sizes": [4]},
        }
        schedule = exp1.build_experiment_schedule(config, seeds=(9000, 9001))
        self.assertEqual(
            tuple((run.run_sequence, run.num_agents, run.seed) for run in schedule),
            ((1, 4, 9000), (2, 4, 9001)),
        )

    def test_preparation_failure_has_no_verified_formation_latency(self) -> None:
        self.assertIsNone(
            exp1.measured_formation_latency(
                task_received_at=10.0,
                verified_at=10.0004,
                failure_stage="PREPARATION",
            )
        )
        self.assertAlmostEqual(
            exp1.measured_formation_latency(
                task_received_at=10.0,
                verified_at=13.1,
                failure_stage="PING_VERIFICATION",
            ),
            3.1,
        )

    def test_formal_exit_code_rejects_failures_and_missing_trials(self) -> None:
        config = {
            "experiment": {"phase": "formal"},
            "simulation": {"task_sizes": [4]},
            "methods": ["proposed", "cspf", "global_sfc_embedding"],
        }
        successful = [SimpleNamespace(success=True) for _ in range(3)]
        failed = [SimpleNamespace(success=True), SimpleNamespace(success=False), SimpleNamespace(success=True)]
        self.assertEqual(exp1.formal_run_exit_code(config, successful, seeds=(0,)), 0)
        self.assertEqual(exp1.formal_run_exit_code(config, failed, seeds=(0,)), 1)
        self.assertEqual(exp1.formal_run_exit_code(config, successful[:2], seeds=(0,)), 1)
        pilot = {
            "experiment": {"phase": "pilot"},
            "simulation": {"task_sizes": [4]},
            "methods": ["proposed", "cspf", "global_sfc_embedding"],
        }
        self.assertEqual(
            exp1.formal_run_exit_code(
                pilot, failed, seeds=(9000,), require_all_success=True
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
