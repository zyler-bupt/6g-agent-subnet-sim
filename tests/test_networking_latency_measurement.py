from __future__ import annotations

import asyncio
import csv
import tempfile
import unittest
from pathlib import Path

from experiments.measure_networking_latency import (
    measure_networking_latency,
    render_report,
    write_csv,
)


class NetworkingLatencyMeasurementTests(unittest.TestCase):
    def test_measurement_reports_networking_latency_samples(self) -> None:
        result = asyncio.run(measure_networking_latency(rounds=3, warmup=1))
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(len(result["samples"]), 3)
        self.assertTrue(result["summary"]["success"])
        for sample in result["samples"]:
            self.assertTrue(sample["success"])
            self.assertEqual(sample["subnet_state"], "networked")
            self.assertGreaterEqual(sample["controller_build_ms"], 0.0)
            self.assertEqual(sample["controller_build_ms"], sample["networking_latency_ms"])
            self.assertEqual(sample["session_count"], 3)
            self.assertEqual(sample["involved_gateway_count"], 3)
            self.assertGreater(sample["route_count"], 0)

    def test_render_report_states_measurement_scope(self) -> None:
        result = asyncio.run(measure_networking_latency(rounds=1, warmup=0))
        text = render_report(result)
        self.assertIn("Controller 侧任务子网构建时延测量", text)
        self.assertIn("controller.build_task_subnet(task)", text)
        self.assertIn("不包含", text)
        self.assertIn("不是端到端组网时延", text)

    def test_write_csv_exports_samples(self) -> None:
        result = asyncio.run(measure_networking_latency(rounds=2, warmup=0))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "networking-latency.csv"
            write_csv(path, result["samples"])
            with path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertIn("controller_build_ms", rows[0])


if __name__ == "__main__":
    unittest.main()
