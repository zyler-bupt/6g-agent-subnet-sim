from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from sanet_dual.data import DualAgentScaler, build_windows, load_dual_agent_series


class SANetDualDataTests(unittest.TestCase):
    def test_official_example_maps_application_and_network_series(self) -> None:
        values = load_dual_agent_series("third_party/SANet/data/example_band_n1")
        app = np.load("third_party/SANet/data/example_band_n1/user_intent.npy")
        network = np.load("third_party/SANet/data/example_band_n1/traffic.npy")
        self.assertEqual(values.shape, (900, 2))
        self.assertAlmostEqual(float(values[0, 0]), float(app[0]), places=5)
        self.assertAlmostEqual(float(values[0, 1]), float(network[0]), places=5)

    def test_scaling_and_windowing_round_trip(self) -> None:
        values = np.arange(80, dtype=np.float32).reshape(40, 2)
        scaler = DualAgentScaler.fit(values)
        normalized = scaler.transform(values)
        restored = scaler.inverse_transform(normalized)
        np.testing.assert_allclose(restored, values, rtol=1e-5, atol=1e-5)
        past, future = build_windows(normalized, input_len=10, pred_len=3, stride=2)
        self.assertEqual(past.shape, (14, 10, 2))
        self.assertEqual(future.shape, (14, 3, 2))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scaler.npz"
            scaler.save(path)
            loaded = DualAgentScaler.load(path)
            np.testing.assert_allclose(loaded.mean, scaler.mean)
            np.testing.assert_allclose(loaded.std, scaler.std)


if __name__ == "__main__":
    unittest.main()
