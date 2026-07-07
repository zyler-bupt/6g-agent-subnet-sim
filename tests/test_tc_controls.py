from __future__ import annotations

import unittest

from testbed.netem import HtbController, NetemController, tc_command


class TcControlTests(unittest.TestCase):
    def test_tc_command_enters_namespace(self) -> None:
        command = tc_command("h-router", "qdisc", "show", sudo=True)
        self.assertEqual(command[:5], ["sudo", "ip", "netns", "exec", "h-router"])
        self.assertEqual(command[5:], ["tc", "qdisc", "show"])

    def test_netem_replace_builds_delay_loss_rate(self) -> None:
        controller = NetemController(namespace="h-router", dev="rt-cloud0")
        commands = controller.commands_for_replace(delay_ms=80, jitter_ms=5, loss_percent=5, rate_mbps=20)

        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertIn("netem", command)
        self.assertIn("80ms", command)
        self.assertIn("5ms", command)
        self.assertIn("5%", command)
        self.assertIn("20mbit", command)

    def test_htb_prioritizes_dscp_with_tos_mask(self) -> None:
        controller = HtbController(
            namespace="h-router",
            dev="rt-cloud0",
            total_mbps=100,
            priority_mbps=80,
            default_mbps=20,
            dry_run=True,
        )
        commands = controller.commands_for_prioritize(0xB8)

        self.assertEqual(len(commands), 5)
        filter_command = commands[-1]
        self.assertIn("filter", filter_command)
        self.assertIn("u32", filter_command)
        self.assertIn("0xb8", filter_command)
        self.assertIn("0xfc", filter_command)
        self.assertEqual(filter_command[-2:], ["flowid", "1:10"])


if __name__ == "__main__":
    unittest.main()
