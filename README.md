# P2P Chat

A minimal, decentralized peer-to-peer messaging system in Python — text **and** voice.  
No central server — every node is both client and server.

## Project Structure

```
p2p_chat/
├── __init__.py      # Package marker
├── __main__.py      # python -m entry point
├── main.py          # Argument parsing & startup
├── node.py          # Core networking, peer management, gossip routing
├── protocol.py      # Wire protocol (length-prefixed JSON over TCP)
├── voice.py         # Voice engine: mic capture, UDP transport, playback
├── cli.py           # Interactive command-line interface
└── utils.py         # IDs, timestamps, colored output helpers
```

## Requirements

- Python 3.10+
- **pyaudio** (for voice chat only): `pip install pyaudio`

Text chat works with no external dependencies. Voice requires PyAudio.

## Quick Start

Open **three** separate terminals and run from the project root
(`P2P_Message/`):

### Terminal 1 — Node A (port 5000)

```bash
python -m p2p_chat --port 5000 --username Alice
```

### Terminal 2 — Node B (port 5001, connects to A)

```bash
python -m p2p_chat --port 5001 --connect 127.0.0.1:5000 --username Bob
```

### Terminal 3 — Node C (port 5002, connects to B)

```bash
python -m p2p_chat --port 5002 --connect 127.0.0.1:5001 --username Carol
```

## CLI Commands

| Command                  | Description                          |
| ------------------------ | ------------------------------------ |
| `connect <host> <port>`  | Connect to a peer                    |
| `send <message>`         | Send a message to the current room   |
| `room [<name>]`          | Show or switch the active room       |
| `peers`                  | List connected peers                 |
| `voice join`             | Join voice chat in the current room  |
| `voice leave`            | Leave voice chat                     |
| `voice mute`             | Mute your microphone                 |
| `voice unmute`           | Unmute your microphone               |
| `voice peers`            | List peers currently in voice        |
| `help`                   | Show available commands              |
| `exit`                   | Shut down the node                   |

## Demo: Multi-Hop Message Propagation

After launching all three nodes (A → B → C topology):

1. On **Node A**, type:
   ```
   send Hello from Alice!
   ```
2. **Node B** receives the message and forwards it.
3. **Node C** displays the message — even though C is only directly
   connected to B, not A.

## Voice Chat

Voice uses UDP for low-latency audio transport and TCP (gossip) for
join/leave signaling.

```
voice join       # start sending/receiving audio in current room
voice mute       # mute mic (still hear others)
voice unmute     # unmute mic
voice leave      # stop voice
voice peers      # see who's in voice
```

### Voice Demo (2 nodes)

```bash
# Terminal 1
python -m p2p_chat --port 5000 --username Alice
> voice join

# Terminal 2
python -m p2p_chat --port 5001 --connect 127.0.0.1:5000 --username Bob
> voice join
```

Both nodes can now talk to each other. The prompt shows `[voice:on]`
when active and `[voice:muted]` when muted.

### Custom UDP Port

By default the UDP port is TCP port + 1000. Override with `--udp-port`:

```bash
python -m p2p_chat --port 5000 --udp-port 7000
```

## Rooms

Each node has one active room (default: `general`).  
Only messages matching your active room are displayed, but **all**
messages are forwarded regardless of room — so the gossip network
stays fully connected.

```
room dev       # switch to #dev
send working on the refactor
room general   # back to #general
```

Voice is also per-room — `voice join` joins voice for your current
room.

## Command-Line Options

```
python -m p2p_chat --help

  --host HOST           Address to bind to (default: 127.0.0.1)
  --port PORT           TCP port to listen on (required)
  --udp-port PORT       UDP port for voice (default: TCP port + 1000)
  --connect HOST:PORT   Peer to connect to on startup (repeatable)
  --username NAME       Display name (default: user-<port>)
```

## Design Notes

- **Wire format**: 4-byte big-endian length prefix + JSON payload.
- **Gossip routing**: Flood with UUID dedup + TTL decrement. Seen IDs
  expire after 2 minutes.
- **Voice transport**: Raw 16-bit PCM at 16 kHz mono over UDP. 20 ms
  frames (320 samples / 640 bytes + 56-byte header = 696 bytes per
  packet, well under MTU).
- **Voice signaling**: `voice_join` / `voice_leave` JSON messages
  gossiped over existing TCP connections. Peers learn each other's UDP
  endpoints from these messages.
- **Audio mixing**: When receiving from multiple peers, PCM samples are
  summed as int32 and clamped to int16 range. No external dependencies
  (uses stdlib `array` module).
- **Thread model**: PyAudio capture/playback run in dedicated threads.
  An asyncio send-loop bridges the capture thread to UDP. Received UDP
  packets are mixed and fed to the playback thread via a queue.
