"""
Entry point for a P2P chat node.

Usage:
    python -m p2p_chat                                     # GUI (startup dialog)
    python -m p2p_chat --gui --port 5000 --username Alice  # GUI (skip dialog)
    python -m p2p_chat --cli --port 5000                   # CLI mode
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading

from .node import Node
from .voice import VoiceEngine


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="P2P Chat – a minimal decentralized messaging node"
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Address to bind to (default: 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port to listen on",
    )
    p.add_argument(
        "--udp-port",
        type=int,
        default=None,
        help="UDP port for voice chat (default: same as TCP port)",
    )
    p.add_argument(
        "--connect",
        metavar="HOST:PORT",
        action="append",
        default=[],
        help="Peer to connect to on startup (can be repeated)",
    )
    p.add_argument(
        "--username",
        default=None,
        help="Display name (defaults to user-<port>)",
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--gui",
        action="store_true",
        help="Launch GUI directly (requires --port)",
    )
    mode.add_argument(
        "--cli",
        action="store_true",
        help="Launch CLI mode (requires --port)",
    )
    return p.parse_args()


def _parse_target(target: str) -> tuple[str, int] | None:
    if ":" not in target:
        return None
    host, port_str = target.rsplit(":", 1)
    try:
        return host, int(port_str)
    except ValueError:
        return None


# ── CLI mode ────────────────────────────────────────────────────────

async def async_main(args: argparse.Namespace) -> None:
    from .cli import CLI

    username = args.username or f"user-{args.port}"
    udp_port = args.udp_port or args.port
    is_host = len(args.connect) == 0
    node = Node(args.host, args.port, username, is_host=is_host)

    await node.start()

    for target in args.connect:
        parsed = _parse_target(target)
        if not parsed:
            print(f"Invalid peer address (expected host:port): {target}")
            continue
        await node.connect_to(*parsed)

    cli = CLI(node, udp_port)
    await cli.run()


# ── GUI mode (with --port already provided) ─────────────────────────

def run_gui_direct(args: argparse.Namespace) -> None:
    from .gui import ChatGUI

    username = args.username or f"user-{args.port}"
    udp_port = args.udp_port or args.port

    is_host = len(args.connect) == 0
    node = Node(args.host, args.port, username, is_host=is_host)
    voice = VoiceEngine(node.node_id, username, args.host, udp_port,
                        is_host=is_host)

    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run_loop, daemon=True, name="asyncio")
    thread.start()

    async def _startup() -> None:
        await node.start()
        await voice.start()
        for target in args.connect:
            parsed = _parse_target(target)
            if not parsed:
                continue
            await node.connect_to(*parsed)

    future = asyncio.run_coroutine_threadsafe(_startup(), loop)
    future.result(timeout=10)

    gui = ChatGUI(node, voice, loop)
    gui.run()

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=3)


# ── Entry point ─────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    try:
        if args.cli:
            # Explicit CLI mode — --port is required.
            if args.port is None:
                print("Error: --cli requires --port", file=sys.stderr)
                sys.exit(1)
            asyncio.run(async_main(args))

        elif args.gui:
            # Explicit GUI mode with --port — skip startup dialog.
            if args.port is None:
                print("Error: --gui requires --port", file=sys.stderr)
                sys.exit(1)
            run_gui_direct(args)

        elif args.port is not None:
            # Backwards-compat: if --port is given without --gui/--cli,
            # default to CLI (original behaviour).
            asyncio.run(async_main(args))

        else:
            # No flags, no port → launch GUI with startup dialog.
            from .gui import launch_gui
            launch_gui()

    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
