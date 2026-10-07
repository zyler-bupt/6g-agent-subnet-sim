"""Window ③: PhyAgent access capacity [SIM] and NetAgent link utilization."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtWidgets

from src.tcanet.prototype.ui import theme
from src.tcanet.prototype.ui.common import DemoWindow, make_plot, run_window
from src.tcanet.prototype.ui.model import AccessModel
from src.tcanet.scenario_fig1 import load

WINDOW_S = 60.0


class PhyWindow(DemoWindow):
    capture_name = "phy"
    topics = ("report.access", "report.link")

    def __init__(self) -> None:
        super().__init__("TCANet · PhyAgent access [SIM] / NetAgent links [MEASURED]")
        world, _task = load("paper_fig1")
        self.gateways = list(world.graph.gateways)
        self.links = sorted(link.link_id for link in world.graph.links)
        self.model = AccessModel(self.gateways, self.links)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        self.cap_plot = make_plot("Access capacity per gateway — PhyAgent [SIM model]", "Mbps")
        self.util_plot = make_plot("Gateway-link utilization — NetAgent", "utilization")
        self.cap_plot.setYRange(0, 75)
        self.util_plot.setLabel("bottom", "link", color=theme.MUTED)
        self.util_plot.setYRange(0, 1.05)
        self.util_plot.getAxis("bottom").setTicks([[(i, link) for i, link in enumerate(self.links)]])
        layout.addWidget(self.cap_plot)
        layout.addWidget(self.util_plot)
        self.setCentralWidget(central)
        self.curves = {
            gw: self.cap_plot.plot([], [], pen=pg.mkPen(theme.series_color(i), width=2), name=gw)
            for i, gw in enumerate(self.gateways)
        }
        self.bars = pg.BarGraphItem(x=list(range(len(self.links))), height=[0] * len(self.links),
                                    width=0.6, brush=theme.SERIES[0])
        self.util_plot.addItem(self.bars)
        self.down_labels = []
        self.resize(900, 640)

    def on_message(self, msg: dict) -> None:
        self.model.apply(msg)

    def refresh(self) -> None:
        latest = 0.0
        for gw, curve in self.curves.items():
            xs, ys = self.model.capacity_series(gw)
            curve.setData(xs, ys)
            if xs:
                latest = max(latest, xs[-1])
        self.cap_plot.setXRange(max(0.0, latest - WINDOW_S), latest + 2.0, padding=0)
        heights = [self.model.utilization[link] if self.model.link_up[link] else 0.0 for link in self.links]
        brushes = [theme.SERIES[0] if self.model.link_up[link] else theme.CRITICAL for link in self.links]
        self.bars.setOpts(height=heights, brushes=brushes)
        for item in self.down_labels:
            self.util_plot.removeItem(item)
        self.down_labels = []
        for index, link in enumerate(self.links):
            text = "DOWN" if not self.model.link_up[link] else f"{heights[index] * 100:.0f}%"
            label = pg.TextItem(text, color=theme.CRITICAL if text == "DOWN" else theme.INK_2, anchor=(0.5, 1))
            label.setPos(index, max(heights[index], 0.0) + 0.04)
            self.util_plot.addItem(label)
            self.down_labels.append(label)


def main() -> None:
    run_window(PhyWindow)


if __name__ == "__main__":
    main()
