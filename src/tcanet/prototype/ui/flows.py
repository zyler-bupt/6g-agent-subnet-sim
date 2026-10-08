"""Window ②: per-dependency goodput and one-way delay (TransAgent/NetAgent view)."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtWidgets

from src.tcanet.prototype.ui import theme
from src.tcanet.prototype.ui.common import DemoWindow, make_plot, run_window
from src.tcanet.prototype.ui.model import FlowModel
from src.tcanet.scenario_fig1 import load

WINDOW_S = 60.0
_MARKER_COLOR = {"event": theme.CRITICAL, "rollback": theme.WARNING, "commit": theme.GOOD, "ops": theme.MUTED}


class FlowsWindow(DemoWindow):
    capture_name = "flows"
    topics = ("report.flow", "ctrl.log", "ops.fault")

    def __init__(self) -> None:
        super().__init__("TCANet · TransAgent / NetAgent — task flows")
        _world, task = load("paper_fig1")
        self.deps = [dep.dep_id for dep in task.dag.dependencies]
        self.names = {dep.dep_id: f"{dep.dep_id} {dep.source}→{dep.target}" for dep in task.dag.dependencies}
        self.model = FlowModel(self.deps)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        self.rx_plot = make_plot("Goodput per dependency", "Mbps")
        self.owd_plot = make_plot("One-way delay per dependency", "ms")
        layout.addWidget(self.rx_plot)
        layout.addWidget(self.owd_plot)
        self.setCentralWidget(central)
        self.curves = {}
        self.labels = {}
        for index, dep in enumerate(self.deps):
            pen = pg.mkPen(theme.series_color(index), width=2)
            for kind, plot in (("rx", self.rx_plot), ("owd", self.owd_plot)):
                self.curves[(dep, kind)] = plot.plot([], [], pen=pen, name=self.names[dep])
                label = pg.TextItem(color=theme.INK_2, anchor=(0, 0.5))
                plot.addItem(label)
                self.labels[(dep, kind)] = label
        self.marker_items: list = []
        self.resize(900, 640)

    def on_message(self, msg: dict) -> None:
        self.model.apply(msg)

    def refresh(self) -> None:
        source = "SIMULATION" if self.model.source == "SIM" else self.model.source
        self.setWindowTitle(f"{self.base_title} [{source}]")
        self.rx_plot.setTitle(f"Goodput per dependency  [{source}]", color=theme.INK, size="11pt")
        self.owd_plot.setTitle(f"One-way delay per dependency  [{source}]", color=theme.INK, size="11pt")
        latest = 0.0
        for (dep, kind), curve in self.curves.items():
            xs, ys = self.model.series(dep, kind)
            curve.setData(xs, ys, connect="finite")
            if xs and ys[-1] == ys[-1]:
                latest = max(latest, xs[-1])
                unit = "Mbps" if kind == "rx" else "ms"
                self.labels[(dep, kind)].setText(f"{dep} {ys[-1]:.1f} {unit}")
                self.labels[(dep, kind)].setPos(xs[-1], ys[-1])
        for plot in (self.rx_plot, self.owd_plot):
            plot.setXRange(max(0.0, latest - WINDOW_S), latest + 8.0, padding=0)
        for item in self.marker_items:
            item.getViewBox() and item.getViewBox().removeItem(item)
        self.marker_items = []
        for index, (t, label, kind) in enumerate(self.model.markers):
            if t < latest - WINDOW_S:
                continue
            for plot, show_label in ((self.rx_plot, True), (self.owd_plot, False)):
                line = pg.InfiniteLine(
                    pos=t, angle=90, pen=pg.mkPen(_MARKER_COLOR[kind], width=1, style=pg.QtCore.Qt.DashLine),
                    label=label if show_label else None,
                    labelOpts={"color": theme.INK_2, "position": 0.9 - 0.18 * (index % 4),
                               "rotateAxis": (1, 0)},
                )
                plot.addItem(line)
                self.marker_items.append(line)


def main() -> None:
    run_window(FlowsWindow)


if __name__ == "__main__":
    main()
