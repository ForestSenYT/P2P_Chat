"""
Interactive CLI for the P2P chat node.

Commands:
    connect <host> <port>   – connect to a remote peer
    send <message>          – broadcast a chat message to the current room
    room <name>             – switch to a different room
    nick <name>             – change display name
    key <passphrase>        – set encryption key for current room
    key                     – show / clear encryption status
    peers                   – list connected peers
    voice <sub>             – voice chat (join/leave/mute/unmute/peers)
    help                    – show available commands
    exit / quit             – shut down the node
"""

from __future__ import annotations

import asyncio
import sys

from .crypto import decrypt, derive_key, encrypt
from .node import Node
from .protocol import chat_message
from .utils import (
    BOLD,
    RESET,
    color,
    fmt_chat,
    fmt_error,
    fmt_peer,
    fmt_room,
    fmt_system,
    fmt_voice,
    monotonic_ms,
    FG_BLUE,
    FG_CYAN,
)
from .voice import VoiceEngine

DEFAULT_ROOM = "general"
DEFAULT_TTL = 5


class CLI:
    """Drives the interactive command loop and wires up display callbacks."""

    def __init__(self, node: Node, udp_port: int):
        self.node = node
        self.room = DEFAULT_ROOM
        self.voice = VoiceEngine(
            node.node_id, node.username, node.host, udp_port
        )
        # Per-room encryption keys: room → derived AES key bytes.
        self._room_keys: dict[str, bytes] = {}

        # Register callbacks on the node.
        self.node.on_message = self._display_message
        self.node.on_voice_signal = self._handle_voice_signal
        self.node.on_peer_disconnect = self.voice.handle_peer_disconnect
        self.node.on_system_message = lambda msg: print(fmt_system(msg))

    # ── Encryption helpers ──────────────────────────────────────────

    def _encrypt_text(self, plaintext: str, room: str) -> str | None:
        key = self._room_keys.get(room)
        if key is None:
            return None
        return encrypt(plaintext, key)

    def _decrypt_msg(self, msg: dict) -> str:
        encrypted = msg.get("encrypted_msg")
        if not encrypted:
            return msg.get("msg", "")
        room = msg.get("room", "")
        key = self._room_keys.get(room)
        if key is None:
            return "[encrypted — no key]"
        plaintext = decrypt(encrypted, key)
        if plaintext is None:
            return "[encrypted — wrong key]"
        return plaintext

    # ── Display ─────────────────────────────────────────────────────

    def _display_message(self, msg: dict) -> None:
        """Called by the node layer for every new inbound message."""
        if msg.get("type") != "chat":
            return
        if msg.get("room") != self.room:
            return

        username = msg.get("username", msg.get("from", "?"))
        ts = msg.get("ts", "")
        text = self._decrypt_msg(msg)
        print(f"\r{fmt_chat(username, text, ts)}")
        self._prompt()

    def _handle_voice_signal(self, msg: dict) -> None:
        """Process voice signaling AND show a notification."""
        self.voice.handle_voice_signal(msg)

        mtype = msg.get("type")
        username = msg.get("username", msg.get("from", "?"))
        room = msg.get("room", "")

        if room != self.room:
            return

        if mtype == "voice_join":
            print(f"\r{fmt_voice(f'{username} joined voice in #{room}')}")
            self._prompt()
        elif mtype == "voice_leave":
            print(f"\r{fmt_voice(f'{username} left voice in #{room}')}")
            self._prompt()

    def _prompt(self) -> None:
        """Re-draw the input prompt."""
        tag = color(f"[{self.room}]", FG_BLUE, bold=True)
        extras = ""
        if self.voice.active_room:
            mic = "muted" if self.voice.muted else "on"
            extras += color(f" [voice:{mic}]", "\033[35m")
        if self.room in self._room_keys:
            extras += color(" [encrypted]", "\033[32m")
        sys.stdout.write(f"{tag}{extras} > ")
        sys.stdout.flush()

    # ── Command dispatch ────────────────────────────────────────────

    async def _handle_line(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return True

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("exit", "quit"):
            return False

        if cmd == "connect":
            await self._cmd_connect(arg)
        elif cmd == "send":
            await self._cmd_send(arg)
        elif cmd == "room":
            self._cmd_room(arg)
        elif cmd == "nick":
            self._cmd_nick(arg)
        elif cmd == "key":
            self._cmd_key(arg)
        elif cmd == "peers":
            self._cmd_peers()
        elif cmd == "voice":
            await self._cmd_voice(arg)
        elif cmd == "help":
            self._cmd_help()
        else:
            print(fmt_error(f"Unknown command: {cmd}. Type 'help' for usage."))
        return True

    # ── Individual commands ─────────────────────────────────────────

    async def _cmd_connect(self, arg: str) -> None:
        tokens = arg.split()
        if len(tokens) != 2:
            print(fmt_error("Usage: connect <host> <port>"))
            return
        host = tokens[0]
        try:
            port = int(tokens[1])
        except ValueError:
            print(fmt_error("Port must be a number."))
            return
        await self.node.connect_to(host, port)

    async def _cmd_send(self, text: str) -> None:
        if not text:
            print(fmt_error("Usage: send <message>"))
            return
        encrypted = self._encrypt_text(text, self.room)
        msg = chat_message(
            self.node.node_id,
            self.room,
            text,
            self.node.username,
            ttl=DEFAULT_TTL,
            encrypted_msg=encrypted,
        )

        print(f"\r{fmt_chat(self.node.username, text, msg['ts'])}")
        await self.node.broadcast(msg)

    def _cmd_room(self, name: str) -> None:
        name = name.strip()
        if not name:
            print(fmt_system(f"Current room: {fmt_room(self.room)}"))
            return
        self.room = name
        enc = " (encrypted)" if self.room in self._room_keys else ""
        print(fmt_system(f"Switched to {fmt_room(self.room)}{enc}"))

    def _cmd_nick(self, name: str) -> None:
        name = name.strip()
        if not name:
            print(fmt_system(f"Current name: {self.node.username}"))
            return
        old = self.node.username
        self.node.username = name
        self.voice.username = name
        print(fmt_system(f"Username changed: {old} → {name}"))

    def _cmd_key(self, passphrase: str) -> None:
        passphrase = passphrase.strip()
        if not passphrase:
            if self.room in self._room_keys:
                del self._room_keys[self.room]
                print(fmt_system(
                    f"Encryption key cleared for {fmt_room(self.room)}."))
            else:
                print(fmt_system(
                    f"No key set for {fmt_room(self.room)}. "
                    "Usage: key <passphrase>"))
            return
        self._room_keys[self.room] = derive_key(passphrase, self.room)
        print(fmt_system(
            f"Encryption key set for {fmt_room(self.room)} (AES-256-GCM)."))

    def _cmd_peers(self) -> None:
        if not self.node.peers:
            print(fmt_system("No connected peers."))
            return
        print(fmt_system(f"Connected peers ({len(self.node.peers)}):"))
        for pid, peer in self.node.peers.items():
            name = f" ({peer.remote_username})" if peer.remote_username else ""
            print(f"  {fmt_peer(pid)}{name}")

    async def _cmd_voice(self, arg: str) -> None:
        parts = arg.strip().split()
        if not parts:
            print(fmt_error("Usage: voice <join|leave|mute|unmute|peers>"))
            return

        sub = parts[0].lower()

        if not self.voice.available:
            print(fmt_error(
                "Voice unavailable. Install pyaudio: pip install pyaudio"))
            return

        if sub == "join":
            if self.voice.active_room:
                print(fmt_error(
                    f"Already in voice for #{self.voice.active_room}. "
                    "Use 'voice leave' first."))
                return
            msg = await self.voice.join(self.room)
    
            await self.node.broadcast(msg)
            print(fmt_voice(f"Joined voice in #{self.room}"))

        elif sub == "leave":
            if not self.voice.active_room:
                print(fmt_error("Not in a voice channel."))
                return
            msg = await self.voice.leave()
    
            await self.node.broadcast(msg)
            print(fmt_voice("Left voice channel."))

        elif sub == "mute":
            self.voice.mute()
            print(fmt_voice("Microphone muted."))

        elif sub == "unmute":
            self.voice.unmute()
            print(fmt_voice("Microphone unmuted."))

        elif sub == "peers":
            peers = self.voice.get_room_peers(self.room)
            if not peers:
                print(fmt_voice(f"No peers in voice for #{self.room}."))
            else:
                print(fmt_voice(
                    f"Voice peers in #{self.room} ({len(peers)}):"))
                for nid, (h, p, uname) in peers.items():
                    print(f"  {fmt_peer(nid)} ({uname}) UDP={h}:{p}")
        else:
            print(fmt_error("Usage: voice <join|leave|mute|unmute|peers>"))

    @staticmethod
    def _cmd_help() -> None:
        print(
            f"""
{BOLD}Available commands:{RESET}
  {color('connect <host> <port>', FG_CYAN)}  Connect to a peer
  {color('send <message>', FG_CYAN)}         Send a message to the current room
  {color('room [<name>]', FG_CYAN)}          Show or switch rooms
  {color('nick <name>', FG_CYAN)}            Change display name
  {color('key <passphrase>', FG_CYAN)}       Set encryption key for current room
  {color('key', FG_CYAN)}                    Clear encryption key
  {color('peers', FG_CYAN)}                  List connected peers
  {color('voice join', FG_CYAN)}             Join voice chat in current room
  {color('voice leave', FG_CYAN)}            Leave voice chat
  {color('voice mute', FG_CYAN)}             Mute your microphone
  {color('voice unmute', FG_CYAN)}           Unmute your microphone
  {color('voice peers', FG_CYAN)}            List peers in voice
  {color('help', FG_CYAN)}                   Show this help
  {color('exit', FG_CYAN)}                   Quit
"""
        )

    # ── Main loop ───────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the interactive command loop (runs until exit)."""
        await self.voice.start()

        print(
            fmt_system(
                f"Node {fmt_peer(self.node.node_id)} ready "
                f"(UDP {self.voice.udp_port}). "
                f"Room: {fmt_room(self.room)}. Type 'help' for commands."
            )
        )

        loop = asyncio.get_event_loop()

        while True:
            self._prompt()
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except EOFError:
                break
            if not line:
                break
            keep_going = await self._handle_line(line)
            if not keep_going:
                break

        print(fmt_system("Shutting down…"))
        await self.voice.stop()
        await self.node.stop()
