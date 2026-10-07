"""``tcanet-demo`` — bring the whole demo up on the Ubuntu desk with one command.

``up`` starts the privileged data plane (``sudo`` + netns, or ``--sim``),
then — as the normal user — the controller terminal, the agent-log tmux
terminal, the operator terminal and the three Qt windows, and tiles them
with ``wmctrl`` (Ubuntu on Xorg).  ``down`` stops everything.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from src.tcanet.prototype import config
from src.tcanet.prototype.runtime import ProcessTable

REPO_ROOT = Path(__file__).resolve().parents[3]
TOP_BAR_PX = 32
TMUX_SESSION = "tcanet-agents"

# key -> (window-title substring used by wmctrl, grid column, grid row)
WINDOWS = {
    "controller": ("TCANet Controller", 0, 0),
    "flows": ("task flows", 1, 0),
    "phy": ("PhyAgent access", 2, 0),
    "dag": ("task DAG", 0, 1),
    "agents": ("TCANet Agents", 1, 1),
    "operator": ("TCANet Operator", 2, 1),
}
QT_WINDOWS = {"flows": "src.tcanet.prototype.ui.flows", "phy": "src.tcanet.prototype.ui.phy",
              "dag": "src.tcanet.prototype.ui.app_dag"}


def parse_dimensions(xdpyinfo_output: str) -> tuple[int, int] | None:
    match = re.search(r"dimensions:\s+(\d+)x(\d+)", xdpyinfo_output)
    return (int(match.group(1)), int(match.group(2))) if match else None


def screen_size(override: str | None) -> tuple[int, int]:
    if override:
        width, height = override.lower().split("x")
        return int(width), int(height)
    if shutil.which("xdpyinfo"):
        found = parse_dimensions(subprocess.run(["xdpyinfo"], capture_output=True, text=True).stdout)
        if found:
            return found
    return 1920, 1080


def layout(width: int, height: int) -> dict[str, tuple[int, int, int, int]]:
    """3×2 grid below the Ubuntu top bar: key -> (x, y, w, h)."""
    cell_w, cell_h = width // 3, (height - TOP_BAR_PX) // 2
    return {key: (col * cell_w, TOP_BAR_PX + row * cell_h, cell_w, cell_h)
            for key, (_title, col, row) in WINDOWS.items()}


def wmctrl_commands(geometry: dict[str, tuple[int, int, int, int]]) -> list[list[str]]:
    cmds = []
    for key, (x, y, w, h) in geometry.items():
        title = WINDOWS[key][0]
        cmds.append(["wmctrl", "-r", title, "-b", "remove,maximized_vert,maximized_horz"])
        cmds.append(["wmctrl", "-r", title, "-e", f"0,{x},{y},{w},{h}"])
    return cmds


def terminal_argv(title: str, command: str) -> list[str]:
    script = f"printf '\\033]0;{title}\\007'; cd {shlex.quote(str(REPO_ROOT))}; {command}; exec bash"
    return ["gnome-terminal", f"--title={title}", "--", "bash", "-lc", script]


def agents_tmux_script(python: str) -> str:
    view = f"{shlex.quote(python)} -m src.tcanet.prototype.ui.console agents --layer"
    return " && ".join([
        f"tmux kill-session -t {TMUX_SESSION} 2>/dev/null; tmux new-session -d -s {TMUX_SESSION} '{view} application'",
        f"tmux split-window -h -t {TMUX_SESSION} '{view} transport'",
        f"tmux split-window -v -t {TMUX_SESSION}:0.0 '{view} network'",
        f"tmux split-window -v -t {TMUX_SESSION}:0.1 '{view} physical'",
        f"tmux select-layout -t {TMUX_SESSION} tiled",
        f"tmux attach -t {TMUX_SESSION}",
    ])


def terminal_specs(python: str, *, auto_capture: bool) -> dict[str, list[str]]:
    controller = f"{shlex.quote(python)} -m src.tcanet.prototype.controller" + (" --auto-capture" if auto_capture else "")
    return {
        "controller": terminal_argv("TCANet Controller", controller),
        "agents": terminal_argv("TCANet Agents", agents_tmux_script(python)),
        "operator": terminal_argv("TCANet Operator", f"{shlex.quote(python)} -m src.tcanet.prototype.ui.console operator"),
    }


def _user_table() -> ProcessTable:
    return ProcessTable(config.run_dir() / "user-pids")


def up(args: argparse.Namespace) -> None:
    python = sys.executable
    sim = args.sim or platform.system() != "Linux"
    env = dict(os.environ)
    if sim:
        subprocess.run([python, "-m", "src.tcanet.prototype.root", "up", "--mode", "sim"], check=True, cwd=REPO_ROOT, env=env)
    else:
        subprocess.run(["sudo", "-E", python, "-m", "src.tcanet.prototype.root", "up", "--mode", "netns"],
                       check=True, cwd=REPO_ROOT, env=env)
    table = _user_table()
    logs = config.logs_dir()
    for key, module in QT_WINDOWS.items():
        table.start(f"ui-{key}", [python, "-m", module], log_path=logs / f"ui-{key}.log", cwd=REPO_ROOT, env=env)
    if shutil.which("gnome-terminal"):
        for key, argv in terminal_specs(python, auto_capture=args.auto_capture).items():
            subprocess.Popen(argv, cwd=REPO_ROOT, env=env, start_new_session=True)
    else:
        table.start("controller", [python, "-m", "src.tcanet.prototype.controller"],
                    log_path=logs / "controller.log", cwd=REPO_ROOT, env=env)
        print("gnome-terminal not found: controller log -> " + str(logs / "controller.log"))
        print(f"agent logs: {python} -m src.tcanet.prototype.ui.console agents --layer network")
    if not args.no_layout and shutil.which("wmctrl"):
        time.sleep(3.0)
        for cmd in wmctrl_commands(layout(*screen_size(args.screen))):
            subprocess.run(cmd, check=False)
    print("demo up — operator commands: tcanetctl submit | demand e1 25 | fail gateway G2 | reset")


def down(_args: argparse.Namespace) -> None:
    _user_table().stop_all()
    subprocess.run(["pkill", "-f", "src.tcanet.prototype.controller"], check=False)
    subprocess.run(["pkill", "-f", "src.tcanet.prototype.ui.console"], check=False)
    if shutil.which("tmux"):
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False, capture_output=True)
    if config.read_mode() == "netns" and platform.system() == "Linux":
        subprocess.run(["sudo", "-E", sys.executable, "-m", "src.tcanet.prototype.root", "down"], cwd=REPO_ROOT, check=False)
    else:
        subprocess.run([sys.executable, "-m", "src.tcanet.prototype.root", "down"], cwd=REPO_ROOT, check=False)


def record(args: argparse.Namespace) -> None:
    table = _user_table()
    if args.action == "stop":
        table.kill("recorder", 2)  # SIGINT lets ffmpeg finalize the file
        return
    width, height = screen_size(args.screen)
    out = config.run_dir() / f"demo-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    table.start("recorder", ["ffmpeg", "-y", "-f", "x11grab", "-framerate", "15", "-video_size",
                             f"{width}x{height}", "-i", os.environ.get("DISPLAY", ":0"), "-c:v", "libx264",
                             "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)],
                log_path=config.logs_dir() / "recorder.log")
    print(f"recording → {out}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tcanet-demo", description="TCANet live prototype demo")
    sub = parser.add_subparsers(dest="op", required=True)
    p = sub.add_parser("up")
    p.add_argument("--sim", action="store_true", help="simulated data plane (no root)")
    p.add_argument("--no-layout", action="store_true")
    p.add_argument("--auto-capture", action="store_true", help="capture windows at formed/fault/recovered")
    p.add_argument("--screen", help="override screen size, e.g. 2560x1440")
    sub.add_parser("down")
    sub.add_parser("status")
    p = sub.add_parser("capture")
    p.add_argument("tag")
    p = sub.add_parser("record")
    p.add_argument("action", choices=["start", "stop"])
    p.add_argument("--screen")
    args = parser.parse_args()
    if args.op == "up":
        up(args)
    elif args.op == "down":
        down(args)
    elif args.op == "status":
        from src.tcanet.prototype.ctl import main as ctl_main
        ctl_main(["status"])
    elif args.op == "capture":
        from src.tcanet.prototype.capture import capture
        print("\n".join(capture(args.tag)))
    elif args.op == "record":
        record(args)


if __name__ == "__main__":
    main()
