"""
Core node: TCP server + peer connection management.

Star topology:
  - Host: listens for clients, relays every message to all other clients.
  - Client: connects to the host, sends/receives — does NOT relay.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .protocol import decode, encode, peer_hello, peer_list
from .utils import fmt_error, fmt_peer, fmt_system, monotonic_ms

log = logging.getLogger(__name__)


class Peer:
    """Represents one TCP connection to a remote node."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        remote_id: str | None = None,
    ):
        self.reader = reader
        self.writer = writer
        self.remote_id = remote_id
        self.remote_username: str | None = None
        self.real_ip: str | None = None   # actual IP from socket
        self.real_port: int | None = None  # actual port from socket
        self.send_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._closed = False

    @property
    def addr(self) -> str:
        return self.remote_id or "<unknown>"

    async def send(self, msg: dict) -> None:
        await self.send_queue.put(msg)

    async def _writer_loop(self) -> None:
        try:
            while not self._closed:
                msg = await self.send_queue.get()
                if msg is None:
                    break
                self.writer.write(encode(msg))
                await self.writer.drain()
        except (ConnectionError, OSError):
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.send_queue.put_nowait(None)
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class Node:
    """A chat node — either a channel host or a client."""

    def __init__(self, host: str, port: int, username: str,
                 is_host: bool = False):
        self.host = host
        self.port = port
        self.username = username
        self.node_id = f"{host}:{port}"
        self.is_host = is_host

        self.peers: dict[str, Peer] = {}

        # Callbacks (set by UI layer).
        self.on_message: Callable[[dict], None] | None = None
        self.on_voice_signal: Callable[[dict], None] | None = None
        self.on_peer_disconnect: Callable[[str], None] | None = None
        self.on_system_message: Callable[[str], None] | None = None
        # Called when a new peer joins (host can sync room list).
        self.on_peer_join: Callable[[Peer], None] | None = None

        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    def _notify(self, text: str) -> None:
        if self.on_system_message:
            self.on_system_message(text)
        else:
            print(fmt_system(text))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── Public API ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Start listening (host always listens; client doesn't need to)."""
        if self.is_host:
            self._server = await asyncio.start_server(
                self._handle_inbound, self.host, self.port
            )
            self._notify(f"Channel hosted on port {self.port}")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for peer in list(self.peers.values()):
            await peer.close()
        self.peers.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def connect_to(self, host: str, port: int) -> None:
        """Connect to a remote host (used by clients)."""
        target = f"{host}:{port}"
        if target == self.node_id:
            self._notify("Cannot connect to self.")
            return
        if target in self.peers:
            self._notify(f"Already connected to {target}")
            return

        try:
            reader, writer = await asyncio.open_connection(host, port)
        except (ConnectionError, OSError) as exc:
            self._notify(f"Could not connect to {target}: {exc}")
            return

        peer = Peer(reader, writer, remote_id=target)
        peer.real_ip = host
        peer.real_port = port
        self.peers[target] = peer
        self._notify(f"Connected to {target}")

        await peer.send(peer_hello(self.node_id, self.username))
        self._spawn(self._peer_reader(peer))
        self._spawn(peer._writer_loop())

    async def broadcast_peer_list(self) -> None:
        """Host sends the full member list (including itself) to all clients."""
        if not self.is_host:
            return
        members = [{"node_id": self.node_id, "username": self.username}]
        for pid, p in self.peers.items():
            members.append({
                "node_id": pid,
                "username": p.remote_username or pid,
            })
        msg = peer_list(self.node_id, members)
        await self.broadcast(msg)

    async def broadcast(self, msg: dict, exclude: str | None = None) -> None:
        """Send *msg* to every connected peer except *exclude*."""
        for pid, peer in list(self.peers.items()):
            if pid == exclude:
                continue
            try:
                await peer.send(msg)
            except (ConnectionError, OSError):
                await self._remove_peer(peer)

    # ── Internals ───────────────────────────────────────────────────

    async def _handle_inbound(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept a new incoming client connection (host only)."""
        # Get the real remote IP from the socket before anything else.
        peername = writer.get_extra_info("peername")  # (ip, port)
        real_ip = peername[0] if peername else None
        real_port = peername[1] if peername else None

        peer = Peer(reader, writer)
        peer.real_ip = real_ip
        peer.real_port = real_port
        self._spawn(peer._writer_loop())
        try:
            hello = await decode(reader)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            await peer.close()
            return

        if not hello or hello.get("type") != "hello":
            await peer.close()
            return

        remote_id = hello["from"]
        peer.remote_id = remote_id
        peer.remote_username = hello.get("username")

        if remote_id in self.peers:
            await peer.close()
            return

        self.peers[remote_id] = peer
        self._notify(f"{peer.remote_username or remote_id} joined the channel")

        await peer.send(peer_hello(self.node_id, self.username))

        # Let UI sync state to the new peer (e.g. room list).
        if self.on_peer_join:
            self.on_peer_join(peer)

        # Broadcast updated member list to all clients.
        await self.broadcast_peer_list()

        self._spawn(self._peer_reader(peer))

    async def _peer_reader(self, peer: Peer) -> None:
        try:
            while True:
                msg = await decode(peer.reader)
                if msg is None:
                    break
                await self._handle_message(msg, peer)
        except (asyncio.IncompleteReadError, asyncio.CancelledError,
                ConnectionError, OSError):
            pass
        finally:
            await self._remove_peer(peer)

    async def _handle_message(self, msg: dict, sender: Peer) -> None:
        """Process one inbound message."""
        msg_type = msg.get("type")

        if msg_type == "hello":
            sender.remote_username = msg.get("username")
            # Re-key peers dict if the remote's self-reported node_id
            # differs from the address we used to connect.  This ensures
            # voice_join "from" fields match our peers dict keys.
            reported_id = msg.get("from", "")
            if reported_id and reported_id != sender.remote_id:
                old_id = sender.remote_id
                if old_id in self.peers:
                    del self.peers[old_id]
                sender.remote_id = reported_id
                self.peers[reported_id] = sender
            return

        # ── Deliver to local UI ─────────────────────────────────────
        if msg_type in ("voice_join", "voice_leave",
                        "voice_mute_status", "voice_force_mute",
                        "voice_speaking"):
            if self.on_voice_signal:
                self.on_voice_signal(msg)
        if self.on_message:
            self.on_message(msg)

        # ── Relay: only the host forwards to other clients ──────────
        if self.is_host:
            await self.broadcast(msg, exclude=sender.remote_id)

    async def _remove_peer(self, peer: Peer) -> None:
        if peer.remote_id and peer.remote_id in self.peers:
            del self.peers[peer.remote_id]
            name = peer.remote_username or peer.addr
            self._notify(f"{name} left the channel")
            if self.on_peer_disconnect:
                self.on_peer_disconnect(peer.remote_id)
            # Broadcast updated member list.
            await self.broadcast_peer_list()
        await peer.close()
