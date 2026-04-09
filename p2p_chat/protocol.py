"""
Wire protocol: length-prefixed JSON over TCP.

Frame layout:
    [4 bytes big-endian length][JSON payload of that length]

This avoids partial-read / message-boundary issues that plague naive
newline-delimited approaches.
"""

from __future__ import annotations

import json
import struct
from typing import Any

from .utils import make_id, timestamp

# Maximum single frame size (1 MiB) – a sanity guard.
MAX_FRAME = 10 << 20  # 10 MiB — supports base64-encoded images up to ~7 MB
HEADER = struct.Struct("!I")  # 4-byte unsigned int, network byte order

# ── Message constructors ────────────────────────────────────────────


def chat_message(
    node_id: str,
    room: str,
    text: str,
    username: str,
    ttl: int = 5,
    encrypted_msg: str | None = None,
) -> dict[str, Any]:
    """Build a chat message dict ready for the wire.

    When *encrypted_msg* is provided the plaintext *text* is stored only
    for local display; the wire carries the ciphertext in ``encrypted_msg``
    and replaces ``msg`` with a placeholder so nodes without the key can
    still route the message.
    """
    msg: dict[str, Any] = {
        "id": make_id(),
        "type": "chat",
        "from": node_id,
        "username": username,
        "room": room,
        "msg": text if encrypted_msg is None else "[encrypted]",
        "ttl": ttl,
        "ts": timestamp(),
    }
    if encrypted_msg is not None:
        msg["encrypted_msg"] = encrypted_msg
    return msg


def image_message(
    node_id: str,
    room: str,
    username: str,
    filename: str,
    image_b64: str,
    ttl: int = 5,
    encrypted_image: str | None = None,
) -> dict[str, Any]:
    """Build an image message for the wire."""
    msg: dict[str, Any] = {
        "id": make_id(),
        "type": "image",
        "from": node_id,
        "username": username,
        "room": room,
        "filename": filename,
        "image_data": image_b64 if encrypted_image is None else "",
        "ttl": ttl,
        "ts": timestamp(),
    }
    if encrypted_image is not None:
        msg["encrypted_image"] = encrypted_image
    return msg


def peer_hello(node_id: str, username: str) -> dict[str, Any]:
    """Handshake message sent immediately after connecting."""
    return {
        "id": make_id(),
        "type": "hello",
        "from": node_id,
        "username": username,
    }


def voice_join(
    node_id: str,
    username: str,
    room: str,
    udp_host: str,
    udp_port: int,
    ttl: int = 5,
) -> dict[str, Any]:
    """Announce that this node has joined voice in *room*."""
    return {
        "id": make_id(),
        "type": "voice_join",
        "from": node_id,
        "username": username,
        "room": room,
        "udp_host": udp_host,
        "udp_port": udp_port,
        "ttl": ttl,
        "ts": timestamp(),
    }


def voice_leave(
    node_id: str,
    username: str,
    room: str,
    ttl: int = 5,
) -> dict[str, Any]:
    """Announce that this node has left voice in *room*."""
    return {
        "id": make_id(),
        "type": "voice_leave",
        "from": node_id,
        "username": username,
        "room": room,
        "ttl": ttl,
        "ts": timestamp(),
    }


def voice_mute_status(
    node_id: str,
    username: str,
    room: str,
    muted: bool,
) -> dict[str, Any]:
    """Broadcast this node's mute state to all peers."""
    return {
        "id": make_id(),
        "type": "voice_mute_status",
        "from": node_id,
        "username": username,
        "room": room,
        "muted": muted,
        "ts": timestamp(),
    }


def voice_force_mute(
    node_id: str,
    room: str,
    target_node_id: str,
    muted: bool = True,
) -> dict[str, Any]:
    """Host forces a user to mute/unmute."""
    return {
        "id": make_id(),
        "type": "voice_force_mute",
        "from": node_id,
        "room": room,
        "target": target_node_id,
        "muted": muted,
        "ts": timestamp(),
    }


def channel_config(
    node_id: str,
    allow_room_create: bool,
    max_image_size: int = 5 * 1024 * 1024,
) -> dict[str, Any]:
    """Host broadcasts channel settings to all clients."""
    return {
        "id": make_id(),
        "type": "channel_config",
        "from": node_id,
        "allow_room_create": allow_room_create,
        "max_image_size": max_image_size,
        "ts": timestamp(),
    }


def peer_list(node_id: str, peers: list[dict]) -> dict[str, Any]:
    """Host broadcasts the full member list to all clients.

    *peers* is a list of {"node_id": ..., "username": ...} dicts.
    """
    return {
        "id": make_id(),
        "type": "peer_list",
        "from": node_id,
        "peers": peers,
        "ts": timestamp(),
    }


def room_create(node_id: str, username: str, room: str) -> dict[str, Any]:
    """Announce creation of a new room."""
    return {
        "id": make_id(),
        "type": "room_create",
        "from": node_id,
        "username": username,
        "room": room,
        "ts": timestamp(),
    }


def room_delete(node_id: str, username: str, room: str) -> dict[str, Any]:
    """Announce deletion of a room (only the creator may send this)."""
    return {
        "id": make_id(),
        "type": "room_delete",
        "from": node_id,
        "username": username,
        "room": room,
        "ts": timestamp(),
    }


# ── Encode / decode ────────────────────────────────────────────────


def encode(msg: dict[str, Any]) -> bytes:
    """Serialize a message dict into a length-prefixed frame."""
    payload = json.dumps(msg, separators=(",", ":")).encode()
    return HEADER.pack(len(payload)) + payload


async def decode(reader) -> dict[str, Any] | None:
    """
    Read exactly one frame from an asyncio StreamReader.

    Returns the parsed dict, or None on EOF / protocol error.
    """
    header = await reader.readexactly(HEADER.size)
    (length,) = HEADER.unpack(header)
    if length > MAX_FRAME:
        return None
    payload = await reader.readexactly(length)
    return json.loads(payload)
