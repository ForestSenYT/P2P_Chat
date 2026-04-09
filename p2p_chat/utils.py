"""Utility helpers: unique IDs, timestamps, colored terminal output."""

import uuid
import time
from datetime import datetime


def make_id() -> str:
    """Generate a unique message ID."""
    return str(uuid.uuid4())


def timestamp() -> str:
    """ISO-8601 timestamp for the current moment."""
    return datetime.now().isoformat(timespec="seconds")


def monotonic_ms() -> int:
    """Monotonic clock in milliseconds (for cache expiry)."""
    return int(time.monotonic() * 1000)


# ── ANSI colour helpers ─────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

FG_RED = "\033[31m"
FG_GREEN = "\033[32m"
FG_YELLOW = "\033[33m"
FG_BLUE = "\033[34m"
FG_MAGENTA = "\033[35m"
FG_CYAN = "\033[36m"
FG_WHITE = "\033[37m"


def color(text: str, fg: str, bold: bool = False) -> str:
    prefix = (BOLD if bold else "") + fg
    return f"{prefix}{text}{RESET}"


def fmt_peer(addr: str) -> str:
    return color(addr, FG_CYAN)


def fmt_room(room: str) -> str:
    return color(f"#{room}", FG_MAGENTA, bold=True)


def fmt_chat(username: str, msg: str, ts: str) -> str:
    time_part = color(ts, FG_WHITE + DIM)
    user_part = color(username, FG_GREEN, bold=True)
    return f"{time_part} {user_part}: {msg}"


def fmt_system(msg: str) -> str:
    return color(f"[system] {msg}", FG_YELLOW)


def fmt_error(msg: str) -> str:
    return color(f"[error] {msg}", FG_RED)


def fmt_voice(msg: str) -> str:
    return color(f"[voice] {msg}", FG_MAGENTA)
