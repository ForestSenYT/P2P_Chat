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
from .utils import detect_lan_ip
from .voice import VoiceEngine


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="P2P Chat – a minimal decentralized messaging node"
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "TCP bind / client bind address. "
            "CLI host on 127.0.0.1 = localhost only; use 0.0.0.0 for LAN. "
            "GUI --gui host mode upgrades 127.0.0.1 to 0.0.0.0 automatically."
        ),
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
    if is_host and node.host == "0.0.0.0":
        node.node_id = f"{detect_lan_ip()}:{args.port}"

    if is_host and args.host in ("127.0.0.1", "localhost"):
        print(
            "[system] TCP is bound to 127.0.0.1 — only this machine can connect. "
            "Use --host 0.0.0.0 for a LAN-visible channel host.",
            file=sys.stderr,
        )

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
    from .gui import ChatGUI, _ASYNC_SHUTDOWN_TIMEOUT, _STARTUP_TIMEOUT

    username = args.username or f"user-{args.port}"
    udp_port = args.udp_port or args.port

    is_host = len(args.connect) == 0
    bind_host = args.host
    # Startup dialog uses 0.0.0.0 so LAN can connect; --gui defaulted to 127.0.0.1
    # which only accepts local connections — treat that as accidental for "host".
    if is_host and bind_host in ("127.0.0.1", "localhost"):
        bind_host = "0.0.0.0"

    node = Node(bind_host, args.port, username, is_host=is_host)
    if is_host and bind_host == "0.0.0.0":
        node.node_id = f"{detect_lan_ip()}:{args.port}"

    voice = VoiceEngine(node.node_id, username, bind_host, udp_port,
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
    try:
        future.result(timeout=_STARTUP_TIMEOUT)
    except Exception:
        async def _cleanup_partial() -> None:
            try:
                await voice.stop()
            except Exception:
                pass
            try:
                await node.stop()
            except Exception:
                pass

        cf = asyncio.run_coroutine_threadsafe(_cleanup_partial(), loop)
        try:
            cf.result(timeout=_ASYNC_SHUTDOWN_TIMEOUT)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        raise

    gui = ChatGUI(node, voice, loop)
    gui.run()

    if not gui._async_shutdown_ok:
        async def _cleanup_after_gui() -> None:
            try:
                await voice.stop()
            except Exception:
                pass
            try:
                await node.stop()
            except Exception:
                pass

        fut = asyncio.run_coroutine_threadsafe(_cleanup_after_gui(), loop)
        try:
            fut.result(timeout=_ASYNC_SHUTDOWN_TIMEOUT)
        except Exception:
            pass
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


# ── Entry point ─────────────────────────────────────────────────────

def _report_gui_fatal() -> None:
    import traceback

    tb = traceback.format_exc()
    print(tb, file=sys.stderr)
    if getattr(sys, "frozen", False):
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            msg = tb if len(tb) <= 3500 else tb[:3500] + "\n…"
            messagebox.showerror("P2P Chat", msg)
            root.destroy()
        except Exception:
            pass


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
    except Exception:
        _report_gui_fatal()
        sys.exit(1)


if __name__ == "__main__":
    main()
