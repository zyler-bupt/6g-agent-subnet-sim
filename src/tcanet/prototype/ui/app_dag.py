"""Window ④: AppAgent task DAG, per-dependency q^H status, subnet version and Φ."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from src.tcanet.prototype.ui import theme
from src.tcanet.prototype.ui.common import DemoWindow, run_window
from src.tcanet.prototype.ui.model import TaskModel
from src.tcanet.scenario_fig1 import load

# Column/row positions of the Fig. 1 DAG: a1, a2 -> a3 -> a4.
_POS = {"a1": (0, 0), "a2": (0, 2), "a3": (1, 1), "a4": (2, 1)}
_STATUS = {"ok": (theme.GOOD, "q^H met"), "violated": (theme.CRITICAL, "q^H VIOLATED"),
           "inactive": (theme.MUTED, "inactive")}


def _short(agent_id: str | None) -> str:
    """``transport-G1`` -> ``t@G1`` for the binding column."""
    if not agent_id:
        return "—"
    role, _, gateway = agent_id.partition("-")
    return f"{role[0]}@{gateway}"


class DagWindow(DemoWindow):
    capture_name = "app_dag"
    topics = ("ctrl.subnet", "ctrl.log", "report.flow")

    def __init__(self) -> None:
        super().__init__("TCANet · AppAgent — task DAG and subnet state")
        _world, self.task = load("paper_fig1")
        self.model = TaskModel(self.task)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        self.header = QtWidgets.QLabel()
        self.header.setStyleSheet(f"font-size: 16pt; color: {theme.INK}; padding: 4px;")
        layout.addWidget(self.header)
        self.scene = QtWidgets.QGraphicsScene()
        self.view = QtWidgets.QGraphicsView(self.scene)
        self.view.setRenderHint(QtGui.QPainter.Antialiasing)
        self.view.setStyleSheet(f"background: {theme.SURFACE}; border: none;")
        layout.addWidget(self.view, stretch=3)
        self.table = QtWidgets.QTableWidget(len(self.task.dag.dependencies), 5)
        self.table.setHorizontalHeaderLabels(["dep", "path π", "Φ (t, n, p)", "measured", "status"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(f"background: {theme.SURFACE}; color: {theme.INK_2}; gridline-color: {theme.GRID};")
        layout.addWidget(self.table, stretch=2)
        self.setCentralWidget(central)
        self.resize(900, 640)

    def on_message(self, msg: dict) -> None:
        self.model.apply(msg)

    def _node_point(self, agent_id: str) -> QtCore.QPointF:
        col, row = _POS[agent_id]
        return QtCore.QPointF(40 + col * 260, 30 + row * 90)

    def refresh(self) -> None:
        model = self.model
        state = f"v{model.version}" if model.version else "not formed"
        self.header.setText(f"Task {self.task.dag.task_id} · subnet {state}"
                            + (f" · reconfiguring {', '.join(sorted(model.affected))}" if model.affected else ""))
        self.scene.clear()
        names = {ep.agent_id: ep.name for ep in self.task.dag.endpoints}
        for dep in self.task.dag.dependencies:
            color, label = _STATUS[model.status(dep.dep_id)]
            start, end = self._node_point(dep.source), self._node_point(dep.target)
            width = 4 if dep.dep_id in model.affected else 2
            pen = QtGui.QPen(QtGui.QColor(color), width)
            if dep.dep_id in model.affected:
                pen.setStyle(QtCore.Qt.DashLine)
            self.scene.addLine(start.x() + 70, start.y() + 20, end.x(), end.y() + 20, pen)
            mid = (start + end) / 2
            text = self.scene.addText(f"{dep.dep_id}: {label}")
            text.setDefaultTextColor(QtGui.QColor(theme.INK_2))
            text.setPos(mid.x() + 20, mid.y() - 6)
        for agent_id, name in names.items():
            point = self._node_point(agent_id)
            rect = self.scene.addRect(point.x(), point.y(), 140, 40, QtGui.QPen(QtGui.QColor(theme.AXIS)),
                                      QtGui.QBrush(QtGui.QColor(theme.PAGE)))
            rect.setZValue(1)
            text = self.scene.addText(f"{agent_id}  {name}")
            text.setDefaultTextColor(QtGui.QColor(theme.INK))
            text.setPos(point.x() + 4, point.y() + 8)
            text.setZValue(2)
        for row, dep in enumerate(self.task.dag.dependencies):
            sample = model.latest.get(dep.dep_id)
            measured = "—" if sample is None else (
                f"{sample['rx_mbps']:.1f} Mbps / "
                + ("—" if sample.get("owd_ms") is None else f"{sample['owd_ms']:.1f} ms"))
            color, label = _STATUS[model.status(dep.dep_id)]
            cells = [dep.dep_id, "→".join(model.paths.get(dep.dep_id, [])) or "—",
                     ", ".join(_short(a) for a in model.bindings.get(dep.dep_id, [])) or "—",
                     measured, label]
            for col, value in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(value)
                if col == 4:
                    item.setForeground(QtGui.QColor(color))
                self.table.setItem(row, col, item)


def main() -> None:
    run_window(DagWindow)


if __name__ == "__main__":
    main()
