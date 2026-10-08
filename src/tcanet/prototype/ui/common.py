"""Shared window chrome: dark theme, status badge, capture-on-request."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from src.tcanet.prototype import config
from src.tcanet.prototype.ui import theme
from src.tcanet.prototype.ui.qtbus import BusBridge


def make_plot(title: str, y_label: str) -> pg.PlotWidget:
    plot = pg.PlotWidget(background=theme.SURFACE)
    plot.setTitle(title, color=theme.INK, size="11pt")
    plot.showGrid(x=False, y=True, alpha=0.25)
    for side in ("left", "bottom"):
        axis = plot.getAxis(side)
        axis.setPen(pg.mkPen(theme.AXIS))
        axis.setTextPen(pg.mkPen(theme.MUTED))
    plot.setLabel("left", y_label, color=theme.MUTED)
    plot.setLabel("bottom", "time (s)", color=theme.MUTED)
    plot.addLegend(offset=(-10, 6), labelTextColor=theme.INK_2, colCount=4)
    return plot


class DemoWindow(QtWidgets.QMainWindow):
    """Base window: bus bridge, connection badge, ``ui.capture`` handling."""

    capture_name = "window"
    topics: tuple[str, ...] = ()

    def __init__(self, title: str) -> None:
        super().__init__()
        self.base_title = title
        self.setWindowTitle(title)
        self.setStyleSheet(f"QMainWindow, QWidget {{ background: {theme.PAGE}; color: {theme.INK}; }}")
        self.badge = QtWidgets.QLabel("connecting…")
        self.badge.setStyleSheet(f"color: {theme.MUTED}; padding: 2px 6px;")
        self.statusBar().addPermanentWidget(self.badge)
        self.bridge = BusBridge(f"ui-{self.capture_name}", self.topics + ("ui.capture",))
        self.bridge.message.connect(self._on_message)
        self.bridge.connected.connect(self._on_connected)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)

    def start(self) -> None:
        self.bridge.start()

    def _on_connected(self, ok: bool) -> None:
        self.badge.setText("● bus connected" if ok else "○ bus disconnected")
        self.badge.setStyleSheet(f"color: {theme.GOOD if ok else theme.CRITICAL}; padding: 2px 6px;")

    def _on_message(self, msg: dict) -> None:
        if msg["topic"] == "ui.capture":
            self.capture(msg["payload"].get("tag", "manual"))
            return
        self.on_message(msg)

    def on_message(self, msg: dict) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        pass

    def capture(self, tag: str) -> str:
        target = config.captures_dir() / tag
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{self.capture_name}.png"
        self.grab().save(str(path))
        return str(path)


def run_window(window_cls) -> None:
    app = QtWidgets.QApplication([])
    pg.setConfigOptions(antialias=True)
    window = window_cls()
    window.show()
    window.start()
    app.exec()
