"""Protected background traffic (``d^prot_r``) on a gateway link."""
from __future__ import annotations

import argparse
import asyncio
import signal

from src.tcanet.prototype.flow import FlowSender, FlowSink

BACKGROUND_PORT = 46999


async def _run(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    if args.mode == "send":
        await FlowSender(args.dst, args.port, args.mbps).run(stop)
    else:
        await FlowSink(args.port, lambda sample: None, period_s=1.0).run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="protected background traffic")
    parser.add_argument("mode", choices=["send", "sink"])
    parser.add_argument("--dst", default="")
    parser.add_argument("--port", type=int, default=BACKGROUND_PORT)
    parser.add_argument("--mbps", type=float, default=0.0)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
