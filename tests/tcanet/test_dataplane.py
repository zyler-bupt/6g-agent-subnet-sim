"""Dataplane seam: Apply/Rollback and asynchronous Assess (paper Alg. 1)."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from src.tcanet.closure import gateway_failure
from src.tcanet.dataplane import NullDataplane
from src.tcanet.scenario import fail_gateway, run_formation
from src.tcanet.scenario_fig1 import build_world, fig1_task
from src.tcanet.verify import (
    DepObservation,
    RecoveryController,
    RecoveryHooks,
    projected_measurements,
)


class RecordingDataplane(NullDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.applied: list[int] = []

    async def apply(self, staged, world):
        self.applied.append(staged.subnet.version)
        return await super().apply(staged, world)


def _async_l3_lossy(task):
    async def measure(staged, world, dep_ids):
        await asyncio.sleep(0)
        observations = projected_measurements(staged, world, dep_ids, task=task)
        return tuple(
            replace(obs, loss_rate=0.10)
            if "L3" in staged.subnet.paths[obs.dep_id].link_ids
            else obs
            for obs in observations
        )

    return measure


class DataplaneTests(unittest.TestCase):
    def _formed(self):
        world = build_world()
        task = fig1_task(world)
        subnet = asyncio.run(run_formation(task, world)).subnet
        return world, task, subnet

    def test_rejected_window_rolls_back_then_retries(self) -> None:
        world, task, subnet = self._formed()
        fail_gateway(world, "G2")
        dataplane = RecordingDataplane()
        rolled: list[int] = []

        async def on_rollback(attempt):
            rolled.append(attempt.index)

        controller = RecoveryController(
            measure=_async_l3_lossy(task), dataplane=dataplane
        )
        result = asyncio.run(
            controller.recover(
                task, subnet, world, gateway_failure("G2"),
                hooks=RecoveryHooks(on_rollback=on_rollback),
            )
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(dataplane.applied, [2, 2])
        self.assertEqual(dataplane.rollbacks, [2])
        self.assertEqual(rolled, [1])

    def test_formation_rolls_back_when_assess_fails(self) -> None:
        world = build_world()
        task = fig1_task(world)
        dataplane = RecordingDataplane()

        async def silent(staged, world, dep_ids):
            return ()

        outcome = asyncio.run(
            run_formation(task, world, dataplane=dataplane, measure=silent)
        )
        self.assertIsNone(outcome.subnet)
        self.assertEqual(dataplane.applied, [1])
        self.assertEqual(dataplane.rollbacks, [1])

    def test_formation_accepts_with_async_measure(self) -> None:
        world = build_world()
        task = fig1_task(world)

        async def good(staged, world, dep_ids):
            return tuple(
                DepObservation(dep, 100.0, 10.0, 20.0, 0.001) for dep in dep_ids
            )

        outcome = asyncio.run(run_formation(task, world, measure=good))
        self.assertIsNotNone(outcome.subnet)
        self.assertEqual(outcome.subnet.version, 1)


if __name__ == "__main__":
    unittest.main()
