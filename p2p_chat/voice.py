"""
Voice chat engine: mic capture → UDP → remote playback.

Star topology voice:
  - Clients send UDP audio to the host only.
  - Host receives from all clients, mixes, and sends a per-client mix
    (excluding that client's own audio) back to each client.
  - This keeps packet count at O(N) instead of O(N²).
"""

from __future__ import annotations

import array
import asyncio
import collections
import queue
import struct
import threading
import time as _time
from typing import Any

try:
    import pyaudio  # type: ignore
except ImportError:
    pyaudio = None

from .crypto import decrypt_bytes, encrypt_bytes
from .protocol import voice_join, voice_leave
from .utils import detect_lan_ip, fmt_error, fmt_voice

# ── Audio parameters ────────────────────────────────────────────────
RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK = 320            # 20 ms at 16 kHz
CHUNK_BYTES = CHUNK * SAMPLE_WIDTH  # 640 bytes

# ── UDP packet layout ──────────────────────────────────────────────
ROOM_FIELD = 16
ID_FIELD = 36
SEQ_STRUCT = struct.Struct("!I")
VOICE_HEADER = ROOM_FIELD + ID_FIELD + SEQ_STRUCT.size

SILENCE = b"\x00" * CHUNK_BYTES

# int16 peak below this ≈ silence / fan noise; above ≈ "you are speaking" for UI.
_MIC_SPEECH_PEAK_THRESHOLD = 700


def _pcm_peak_abs(pcm: bytes) -> int:
    if len(pcm) < 2:
        return 0
    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return 0
    return max(abs(x) for x in samples)


def _pad(text: str, length: int) -> bytes:
    raw = text.encode("utf-8")[:length]
    return raw + b"\x00" * (length - len(raw))


def _unpad(data: bytes) -> str:
    return data.rstrip(b"\x00").decode("utf-8", errors="replace")


def encode_voice_packet(room: str, node_id: str, seq: int, pcm: bytes) -> bytes:
    return _pad(room, ROOM_FIELD) + _pad(node_id, ID_FIELD) + SEQ_STRUCT.pack(seq) + pcm


def decode_voice_packet(data: bytes) -> tuple[str, str, int, bytes] | None:
    if len(data) < VOICE_HEADER:
        return None
    room = _unpad(data[:ROOM_FIELD])
    node_id = _unpad(data[ROOM_FIELD : ROOM_FIELD + ID_FIELD])
    (seq,) = SEQ_STRUCT.unpack_from(data, ROOM_FIELD + ID_FIELD)
    pcm = data[VOICE_HEADER:]
    return room, node_id, seq, pcm


def _mix_chunks(chunks: list[bytes]) -> bytes:
    if not chunks:
        return SILENCE
    if len(chunks) == 1:
        return chunks[0]
    mixed = array.array("h", chunks[0])
    for chunk in chunks[1:]:
        other = array.array("h", chunk)
        for i in range(min(len(mixed), len(other))):
            mixed[i] = max(-32768, min(32767, mixed[i] + other[i]))
    return mixed.tobytes()


class _VoiceUDP(asyncio.DatagramProtocol):
    def __init__(self, on_recv):
        self._on_recv = on_recv

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._on_recv(data, addr)

    def error_received(self, exc: Exception) -> None:
        pass


# ── Device helpers ──────────────────────────────────────────────────

def list_audio_devices() -> list[dict[str, Any]]:
    if pyaudio is None:
        return []
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        try:
            info = pa.get_device_info_by_index(i)
            devices.append({
                "index": i,
                "name": info.get("name", f"Device {i}"),
                "max_input": info.get("maxInputChannels", 0),
                "max_output": info.get("maxOutputChannels", 0),
            })
        except Exception:
            pass
    pa.terminate()
    return devices


def list_input_devices() -> list[tuple[int, str]]:
    return [(d["index"], d["name"]) for d in list_audio_devices() if d["max_input"] > 0]


def list_output_devices() -> list[tuple[int, str]]:
    return [(d["index"], d["name"]) for d in list_audio_devices() if d["max_output"] > 0]


# ── Main engine ─────────────────────────────────────────────────────

class VoiceEngine:
    """Manages mic capture, UDP transport, and speaker playback.

    In host mode the engine acts as a mixer: it collects audio from all
    clients, mixes per-client streams (excluding the recipient's own
    audio), and distributes the result.  Clients only talk to the host.
    """

    def __init__(self, node_id: str, username: str, host: str, udp_port: int,
                 *, is_host: bool = False):
        self.node_id = node_id
        self.username = username
        self.host = host
        self.udp_port = udp_port
        self.public_host = detect_lan_ip()
        self.is_host = is_host

        self.active_room: str | None = None
        self.muted = False
        self.force_muted = False

        self.input_device: int | None = None
        self.output_device: int | None = None
        self.room_key: bytes | None = None
        self.echo_cancel = False

        # voice_peers: used by clients to know host UDP addr,
        # and by host to know each client's UDP addr.
        self.voice_peers: dict[str, dict[str, tuple[str, int, str]]] = {}

        # Display-only member list (all voice participants, including
        # those we don't have a direct UDP connection to).
        self.voice_members: dict[str, str] = {}  # node_id → username

        # Per-peer state for GUI.
        self._peer_muted: dict[str, bool] = {}
        self._peer_last_active: dict[str, float] = {}
        self._peer_decrypt_ok: dict[str, bool | None] = {}
        # Local mic: last frame level 0..1 (for GUI meter).
        self._mic_level: float = 0.0
        # monotonic timestamp when PCM peak crossed speech threshold.
        self._last_voice_activity: float = 0.0

        self.on_mute_change: Any = None

        self._seq = 0
        self._pa: Any = None
        self._running = False

        self._udp_transport: asyncio.DatagramTransport | None = None
        self._send_task: asyncio.Task | None = None
        self._mix_task: asyncio.Task | None = None
        # Created in start() on the asyncio thread — not in __init__, because
        # the GUI constructs VoiceEngine on the main thread (wrong default loop).
        self._capture_queue: asyncio.Queue[bytes] | None = None
        self._playback_queue: queue.Queue[bytes] = queue.Queue(maxsize=50)

        # Per-sender audio frame queue (for host mixing).
        self._peer_buffers: dict[str, collections.deque[bytes]] = {}
        self._peer_seq: dict[str, int] = {}
        # Per-sender real UDP address (for host to reply).
        self._peer_addrs: dict[str, tuple[str, int]] = {}

        self._capture_thread: threading.Thread | None = None
        self._playback_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        if pyaudio is None:
            print(fmt_error("pyaudio is not installed. Voice chat disabled."))
            return
        self._loop = asyncio.get_running_loop()
        self._capture_queue = asyncio.Queue(maxsize=50)
        transport, _ = await self._loop.create_datagram_endpoint(
            lambda: _VoiceUDP(self._on_udp_recv),
            local_addr=("0.0.0.0", self.udp_port),
        )
        self._udp_transport = transport

    async def stop(self) -> None:
        if self.active_room:
            await self.leave()
        if self._udp_transport:
            self._udp_transport.close()
            self._udp_transport = None

    @property
    def available(self) -> bool:
        return pyaudio is not None and self._udp_transport is not None

    # ── Join / leave ────────────────────────────────────────────────

    async def join(self, room: str) -> dict:
        self.active_room = room
        self._running = True
        self._pa = pyaudio.PyAudio()

        self._stream_ready = threading.Event()

        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="voice-capture")
        self._capture_thread.start()

        self._playback_thread = threading.Thread(
            target=self._playback_loop, daemon=True, name="voice-playback")
        self._playback_thread.start()

        self._send_task = asyncio.create_task(self._send_loop())

        # Host runs a periodic mixer task.
        if self.is_host:
            self._mix_task = asyncio.create_task(self._mix_and_distribute())

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._stream_ready.wait, 3.0)

        # Register ourselves in members list.
        self.voice_members[self.node_id] = self.username

        # Immediately send a keepalive so the host learns our UDP address
        # before we even send the voice_join TCP signal.
        if not self.is_host:
            self._send_keepalive()

        return voice_join(
            self.node_id, self.username, room,
            self.public_host, self.udp_port,
        )

    async def leave(self) -> dict:
        room = self.active_room or ""
        self._running = False
        self.active_room = None
        self._peer_buffers.clear()
        self._peer_seq.clear()
        self._peer_addrs.clear()
        self.voice_members.pop(self.node_id, None)

        for task in (self._send_task, self._mix_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._send_task = None
        self._mix_task = None

        cap_q = self._capture_queue
        if cap_q is not None:
            while True:
                try:
                    cap_q.get_nowait()
                except asyncio.QueueEmpty:
                    break

        if self._capture_thread:
            self._capture_thread.join(timeout=1.0)
            self._capture_thread = None
        if self._playback_thread:
            self._playback_thread.join(timeout=1.0)
            self._playback_thread = None

        if self._pa:
            self._pa.terminate()
            self._pa = None

        return voice_leave(self.node_id, self.username, room)

    def mute(self) -> None:
        self.muted = True
        if self.on_mute_change:
            self.on_mute_change(True)

    def unmute(self) -> None:
        if self.force_muted:
            return
        self.muted = False
        if self.on_mute_change:
            self.on_mute_change(False)

    # ── Signaling handlers ──────────────────────────────────────────

    def handle_voice_signal(self, msg: dict) -> None:
        mtype = msg.get("type")
        sender = msg.get("from", "")
        room = msg.get("room", "")

        if mtype == "voice_join":
            udp_host = msg.get("udp_host", "")
            udp_port = msg.get("udp_port", 0)
            username = msg.get("username", sender)
            peers = self.voice_peers.setdefault(room, {})
            if sender not in peers:
                peers[sender] = (udp_host, udp_port, username)
            else:
                h, p, _ = peers[sender]
                peers[sender] = (h, p, username)
            self._peer_muted[sender] = False
            self.voice_members[sender] = username

        elif mtype == "voice_leave":
            if room in self.voice_peers:
                self.voice_peers[room].pop(sender, None)
            self._peer_muted.pop(sender, None)
            self._peer_last_active.pop(sender, None)
            self._peer_decrypt_ok.pop(sender, None)
            self.voice_members.pop(sender, None)
            self._peer_addrs.pop(sender, None)

        elif mtype == "voice_mute_status":
            self._peer_muted[sender] = msg.get("muted", False)

        elif mtype == "voice_force_mute":
            target = msg.get("target", "")
            forced = msg.get("muted", True)
            if target == self.node_id:
                self.force_muted = forced
                if forced:
                    self.muted = True
                    if self.on_mute_change:
                        self.on_mute_change(True)

    def handle_peer_disconnect(self, node_id: str) -> None:
        for room_peers in self.voice_peers.values():
            room_peers.pop(node_id, None)
        self._peer_buffers.pop(node_id, None)
        self._peer_seq.pop(node_id, None)
        self._peer_muted.pop(node_id, None)
        self._peer_last_active.pop(node_id, None)
        self._peer_decrypt_ok.pop(node_id, None)
        self.voice_members.pop(node_id, None)
        self._peer_addrs.pop(node_id, None)

    def get_room_peers(self, room: str) -> dict[str, tuple[str, int, str]]:
        return dict(self.voice_peers.get(room, {}))

    def is_peer_speaking(self, node_id: str, threshold: float = 0.5) -> bool:
        last = self._peer_last_active.get(node_id, 0.0)
        return (_time.monotonic() - last) < threshold

    def is_peer_muted(self, node_id: str) -> bool:
        return self._peer_muted.get(node_id, False)

    def is_self_speaking(self, threshold: float = 0.35) -> bool:
        """True if recent mic frames had speech-level energy (not just unmuted)."""
        return (_time.monotonic() - self._last_voice_activity) < threshold

    @property
    def mic_input_level(self) -> float:
        """0..1 peak-based level for local feedback UI (last captured frame)."""
        return self._mic_level

    def is_peer_encrypted(self, node_id: str) -> bool | None:
        return self._peer_decrypt_ok.get(node_id)

    def _enqueue_capture(self, data: bytes) -> None:
        """Thread-safe: put PCM on the asyncio capture queue (same loop as _send_loop)."""
        loop = self._loop
        cap_q = self._capture_queue
        if loop is None or cap_q is None or not self._running:
            return

        def _put() -> None:
            try:
                cap_q.put_nowait(data)
            except asyncio.QueueFull:
                pass

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass

    # ── Capture thread ──────────────────────────────────────────────

    def _capture_loop(self) -> None:
        kwargs: dict[str, Any] = {
            "format": pyaudio.paInt16,
            "channels": CHANNELS,
            "rate": RATE,
            "input": True,
            "frames_per_buffer": CHUNK,
        }
        if self.input_device is not None:
            kwargs["input_device_index"] = self.input_device

        stream = self._pa.open(**kwargs)
        self._stream_ready.set()
        try:
            while self._running:
                data = stream.read(CHUNK, exception_on_overflow=False)
                peak = _pcm_peak_abs(data)
                self._mic_level = min(1.0, peak / 10000.0)
                if self.muted:
                    self._mic_level = 0.0
                    continue
                if peak >= _MIC_SPEECH_PEAK_THRESHOLD:
                    self._last_voice_activity = _time.monotonic()
                self._enqueue_capture(data)
        except OSError:
            pass
        finally:
            stream.stop_stream()
            stream.close()

    # ── Playback thread ─────────────────────────────────────────────

    def _playback_loop(self) -> None:
        kwargs: dict[str, Any] = {
            "format": pyaudio.paInt16,
            "channels": CHANNELS,
            "rate": RATE,
            "output": True,
            "frames_per_buffer": CHUNK,
        }
        if self.output_device is not None:
            kwargs["output_device_index"] = self.output_device

        stream = self._pa.open(**kwargs)
        try:
            while self._running:
                try:
                    data = self._playback_queue.get(timeout=0.05)
                    stream.write(data)
                except queue.Empty:
                    pass
        except OSError:
            pass
        finally:
            stream.stop_stream()
            stream.close()

    # ── Send loop (client → host, or host → own capture) ───────────

    async def _send_loop(self) -> None:
        """Drain capture queue and send to peers via UDP.

        Clients also send a keepalive silence packet every ~1 s when
        muted so the host always knows their UDP address.
        """
        keepalive_interval = 1.0  # seconds
        last_keepalive = 0.0

        cap_q = self._capture_queue
        if cap_q is None:
            return
        while self._running:
            try:
                pcm = await asyncio.wait_for(cap_q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                # No capture data (muted or idle).  Send keepalive if
                # enough time has passed so the host learns our address.
                if not self.is_host:
                    now = _time.monotonic()
                    if now - last_keepalive >= keepalive_interval:
                        last_keepalive = now
                        self._send_keepalive()
                continue

            room = self.active_room
            if not room or not self._udp_transport:
                continue
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            payload = encrypt_bytes(pcm, self.room_key) if self.room_key else pcm
            packet = encode_voice_packet(room, self.node_id, self._seq, payload)

            if self.is_host:
                self._peer_buffers.setdefault(
                    self.node_id, collections.deque(maxlen=50)).append(pcm)
            else:
                self._send_to_host(packet)
                last_keepalive = _time.monotonic()

    def _send_to_host(self, packet: bytes) -> None:
        """Send a UDP packet to the host (first peer in voice_peers)."""
        room = self.active_room
        if not room or not self._udp_transport:
            return
        peers = self.voice_peers.get(room, {})
        for nid, (host, port, _) in peers.items():
            if nid == self.node_id:
                continue
            try:
                self._udp_transport.sendto(packet, (host, port))
            except OSError:
                pass
            break  # only send to host (first peer)

    def _send_keepalive(self) -> None:
        """Send a silent packet so the host learns our UDP address."""
        room = self.active_room
        if not room:
            return
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        # Send unencrypted silence — it's just for address discovery,
        # not real audio.  Avoids expensive encryption every second.
        packet = encode_voice_packet(room, self.node_id, self._seq, SILENCE)
        self._send_to_host(packet)

    # ── Host mixer: collect, mix, distribute ────────────────────────

    async def _mix_and_distribute(self) -> None:
        """Host-only: every 20ms, pop one frame per sender, mix, distribute."""
        while self._running:
            await asyncio.sleep(0.02)  # match 20ms audio frame rate
            if not self._udp_transport or not self.active_room:
                continue

            # Pop one frame (FIFO) from each sender's deque.
            frame: dict[str, bytes] = {}
            for sid, dq in list(self._peer_buffers.items()):
                if dq:
                    frame[sid] = dq.popleft()

            if not frame:
                continue

            room = self.active_room

            # For each connected voice client, mix everyone EXCEPT them.
            peers = self.voice_peers.get(room, {})
            for nid in list(peers):
                if nid == self.node_id:
                    continue
                addr = self._peer_addrs.get(nid)
                if not addr:
                    continue

                others = [c for sid, c in frame.items() if sid != nid]
                if not others:
                    continue
                mixed = _mix_chunks(others)

                payload = encrypt_bytes(mixed, self.room_key) if self.room_key else mixed
                self._seq = (self._seq + 1) & 0xFFFFFFFF
                packet = encode_voice_packet(
                    room, self.node_id, self._seq, payload)
                try:
                    self._udp_transport.sendto(packet, addr)
                except OSError:
                    pass

            # Host plays mixed audio locally (all others).
            host_others = [c for sid, c in frame.items()
                           if sid != self.node_id]
            if host_others:
                mixed_for_host = _mix_chunks(host_others)
                try:
                    self._playback_queue.put_nowait(mixed_for_host)
                except queue.Full:
                    pass

    # ── UDP receive callback ────────────────────────────────────────

    def _on_udp_recv(self, data: bytes, addr: tuple) -> None:
        parsed = decode_voice_packet(data)
        if not parsed:
            return
        room, sender, seq, pcm = parsed

        if room != self.active_room:
            return
        if sender == self.node_id:
            return
        last_seq = self._peer_seq.get(sender, 0)
        if seq != 0 and seq <= last_seq:
            return
        self._peer_seq[sender] = seq

        # NAT traversal: remember real address.
        self._peer_addrs[sender] = addr
        room_peers = self.voice_peers.get(room, {})
        if sender in room_peers:
            _, _, uname = room_peers[sender]
            room_peers[sender] = (addr[0], addr[1], uname)

        # Decrypt.
        is_encrypted = len(pcm) != CHUNK_BYTES
        if is_encrypted and self.room_key:
            pcm = decrypt_bytes(pcm, self.room_key)
            if pcm is None:
                self._peer_decrypt_ok[sender] = False
                return
            self._peer_decrypt_ok[sender] = True
        elif is_encrypted:
            self._peer_decrypt_ok[sender] = False
            return
        else:
            self._peer_decrypt_ok[sender] = None

        self._peer_last_active[sender] = _time.monotonic()

        if self.is_host:
            # Host: store in buffer, mixer task will distribute.
            self._peer_buffers.setdefault(
                sender, collections.deque(maxlen=50)).append(pcm)
        else:
            # Client: play directly (already mixed by host).
            try:
                self._playback_queue.put_nowait(pcm)
            except queue.Full:
                pass
