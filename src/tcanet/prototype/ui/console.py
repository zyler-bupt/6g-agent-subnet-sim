"""Terminal views: per-layer agent logs (tmux panes) and the operator banner."""
from __future__ import annotations

import argparse
import asyncio

from rich.console import Console

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient

_COLORS = {"application": "green", "transport": "dark_orange", "network": "dodger_blue1", "physical": "magenta"}
_TITLES = {"application": "AppAgents", "transport": "TransAgents", "network": "NetAgents", "physical": "PhyAgents"}
_LEVEL = {"warn": "bold yellow", "fail": "bold red", "ok": "bold green"}

OPERATOR_BANNER = """[bold]TCANet operator console[/]   (orchestrator input + fault injection)
  tcanetctl submit                 Act 1  form the task subnet
  tcanetctl demand e1 25           Act 2  drone switches to a 25 Mbps stream
  tcanetctl fail gateway G2        Act 3  transit gateway G2 fails
  tcanetctl reset; tcanetctl submit; tcanetctl degrade L3 10; tcanetctl fail gateway G2
                                   Act 4  hidden loss on L3 → Assess fails → Rollback
  tcanetctl kill physical-G4       Act 5  PhyAgent at G4 dies → Φ rebinding
  tcanetctl status | tcanet-demo capture <tag>
  sudo ip netns exec tc-g1 ip rule show      (inspect FT on a gateway)"""


def format_agent_line(payload: dict) -> str:
    color = _COLORS.get(payload["role"], "white")
    style = _LEVEL.get(payload.get("level", "info"), "")
    text = f"[{style}]{payload['text']}[/]" if style else payload["text"]
    return f"[{color}]{payload['agent_id']:>13}[/] {text}"


async def agents_view(layer: str) -> None:
    console = Console(highlight=False)
    console.rule(f"[{_COLORS[layer]}]{_TITLES[layer]}")
    while True:
        try:
            client = await BusClient.connect(config.bus_path(), f"view-{layer}", ("agent.log",), retries=1)
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(1.0)
            continue
        while (msg := await client.recv()) is not None:
            if msg["payload"].get("role") == layer:
                console.print(format_agent_line(msg["payload"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet terminal views")
    sub = parser.add_subparsers(dest="view", required=True)
    sub.add_parser("agents").add_argument("--layer", required=True, choices=sorted(_TITLES))
    sub.add_parser("operator")
    args = parser.parse_args()
    if args.view == "agents":
        asyncio.run(agents_view(args.layer))
    else:
        Console().print(OPERATOR_BANNER)


if __name__ == "__main__":
    main()
