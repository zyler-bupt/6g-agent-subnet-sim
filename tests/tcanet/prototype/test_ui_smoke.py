"""Offscreen render of every Qt window (skipped without PySide6/pyqtgraph)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import pyqtgraph  # noqa: F401
    from PySide6 import QtWidgets
except ImportError:  # pragma: no cover - optional [demo] extra
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, "PySide6/pyqtgraph not installed")
class WindowSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))
        self.env = mock.patch.dict(os.environ, {"TCANET_RUN_DIR": str(self.dir)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _feed(self, window) -> None:
        msgs = [
            {"topic": "ctrl.subnet", "ts": 1.0, "src": "c", "payload": {
                "version": 1, "paths": {"e1": ["G1", "G2", "G4"]},
                "bindings": {"e1": ["transport-G1", "network-G1", "physical-G1"]}}},
            {"topic": "report.flow", "ts": 1.5, "src": "a", "payload": {
                "dep_id": "e1", "rx_mbps": 15.0, "owd_ms": 20.0, "loss": 0.0, "source": "MEASURED"}},
            {"topic": "report.access", "ts": 1.5, "src": "p", "payload": {
                "gateway": "G1", "capacity_mbps": 60.0, "snr_db": 20.0}},
            {"topic": "report.link", "ts": 1.6, "src": "n", "payload": {
                "link_id": "L1", "up": False, "reporter": "network-G1", "utilization": 0.0}},
            {"topic": "ctrl.log", "ts": 2.0, "src": "c", "payload": {
                "step": "commit", "title": "Commit v1", "level": "ok", "lines": [], "data": {}}},
        ]
        for msg in msgs:
            window.on_message(msg)
        window.refresh()

    def test_windows_render_and_capture(self) -> None:
        from src.tcanet.prototype.ui.app_dag import DagWindow
        from src.tcanet.prototype.ui.flows import FlowsWindow
        from src.tcanet.prototype.ui.phy import PhyWindow

        for cls in (FlowsWindow, PhyWindow, DagWindow):
            window = cls()
            self._feed(window)
            path = Path(window.capture("smoke"))
            self.assertTrue(path.exists(), cls.__name__)
            self.assertGreater(path.stat().st_size, 1000)
            window.close()


if __name__ == "__main__":
    unittest.main()
