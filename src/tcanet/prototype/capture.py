"""Screenshot every demo window into ``<run_dir>/captures/<tag>/``.

Qt windows save themselves on ``ui.capture``; terminal windows are grabbed
by X11 window id (``wmctrl -l`` + ImageMagick ``import``).
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient

TERMINALS = {"TCANet Controller": "controller", "TCANet Agents": "agents", "TCANet Operator": "operator"}


def terminal_capture_commands(wmctrl_list: str, target: Path) -> list[list[str]]:
    """``import`` commands for the terminal windows listed by ``wmctrl -l``."""
    cmds = []
    for line in wmctrl_list.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        window_id, title = parts[0], parts[3]
        for prefix, name in TERMINALS.items():
            if title.startswith(prefix):
                cmds.append(["import", "-window", window_id, str(target / f"{name}.png")])
    return cmds


async def _request_qt_capture(tag: str) -> None:
    client = await BusClient.connect(config.bus_path(), "capture")
    await client.publish("ui.capture", {"tag": tag})
    await asyncio.sleep(0.2)
    await client.close()


def capture(tag: str) -> list[str]:
    target = config.captures_dir() / tag
    target.mkdir(parents=True, exist_ok=True)
    asyncio.run(_request_qt_capture(tag))
    done = [f"Qt windows → {target}"]
    if shutil.which("wmctrl") and shutil.which("import"):
        listing = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout
        for cmd in terminal_capture_commands(listing, target):
            subprocess.run(cmd, check=False)
            done.append(cmd[-1])
    else:
        done.append("terminal capture skipped (needs wmctrl + imagemagick)")
    return done
