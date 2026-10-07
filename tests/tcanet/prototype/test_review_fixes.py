"""Regressions from the final review: outage-gap loss and root-owned run dir."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.tcanet.prototype.flow import SinkStats
from src.tcanet.prototype.root import hand_back


class OutageGapTests(unittest.TestCase):
    def test_packets_lost_before_resume_do_not_count_as_loss(self) -> None:
        stats = SinkStats()
        for seq in range(10):
            stats.add(1, seq, 0, 0, 1000)
        stats.snapshot(0.5)
        stats.snapshot(0.5)  # outage: no packets this window
        for seq in range(500, 560):  # stream resumes after the reroute
            stats.add(1, seq, 0, 0, 1000)
        self.assertEqual(stats.snapshot(0.5)["loss"], 0.0)

    def test_first_packets_after_install_are_not_lossy(self) -> None:
        stats = SinkStats()
        stats.snapshot(0.5)  # sink up before any FT rule is installed
        for seq in range(120, 180):
            stats.add(1, seq, 0, 0, 1000)
        self.assertEqual(stats.snapshot(0.5)["loss"], 0.0)


class HandBackTests(unittest.TestCase):
    def test_run_tree_is_returned_to_the_sudo_user(self) -> None:
        root = Path(tempfile.mkdtemp(dir="/tmp"))
        (root / "logs").mkdir()
        (root / "logs" / "bus.log").write_text("x")
        calls = []
        hand_back(root, {"SUDO_UID": "1000", "SUDO_GID": "1000"},
                  chown=lambda path, uid, gid: calls.append((Path(path).name, uid, gid)))
        self.assertEqual(sorted(calls), [("bus.log", 1000, 1000), ("logs", 1000, 1000),
                                         (root.name, 1000, 1000)])
        calls.clear()
        hand_back(root, {}, chown=lambda *a: calls.append(a))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
