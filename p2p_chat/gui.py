"""
Tkinter GUI for the P2P chat node.

Runs tkinter on the main thread and asyncio in a background daemon thread.
Bridging:
    GUI  → asyncio : asyncio.run_coroutine_threadsafe(coro, loop)
    asyncio → GUI  : root.after(0, callback)   (thread-safe)

Quick launch (no CLI args needed):
    from p2p_chat.gui import launch_gui
    launch_gui()
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import os
import random
import threading
import tkinter as tk
from io import BytesIO
from tkinter import filedialog, ttk
from typing import Any

from .crypto import decrypt, decrypt_bytes, derive_key, encrypt, encrypt_bytes
from .node import Node
from .protocol import (channel_config, chat_message, image_message,
                       room_create, room_delete,
                       voice_force_mute, voice_mute_status)
from .utils import detect_lan_ip, timestamp
from .voice import VoiceEngine, list_input_devices, list_output_devices

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None  # type: ignore
    ImageTk = None  # type: ignore

# ── Colour palette (dark theme) ─────────────────────────────────────
BG_DARK = "#1e1e1e"
BG_MID = "#2c2c2c"
BG_LIGHT = "#333333"
FG_DEFAULT = "#d4d4d4"
FG_DIM = "#7f8c8d"
FG_ACCENT = "#569cd6"
FG_GREEN = "#2ecc71"
FG_YELLOW = "#f1c40f"
FG_RED = "#e74c3c"
FG_MAGENTA = "#c678dd"
FG_CYAN = "#56b6c2"
FONT_FAMILY = "Consolas"

# Voice leave() can need ~2s thread joins plus PyAudio teardown; 3s is too tight.
_ASYNC_SHUTDOWN_TIMEOUT = 25.0
_STARTUP_TIMEOUT = 20.0


class ChatGUI:
    """Main GUI window — replaces the CLI as the user-facing layer."""

    def __init__(
        self,
        node: Node,
        voice: VoiceEngine,
        loop: asyncio.AbstractEventLoop,
    ):
        self.node = node
        self.voice = voice
        self.loop = loop
        self.room = "general"
        self._closing = False
        # Set True when WM_DELETE shutdown coroutine finished (avoids duplicate cleanup).
        self._async_shutdown_ok = False

        # Per-room message history so switching rooms keeps context.
        self._room_history: dict[str, list[tuple[str, str]]] = {}
        # Room tracking: all known rooms + who created them.
        self._rooms: set[str] = {"general"}
        # room → creator node_id (so only the creator can delete).
        self._room_owner: dict[str, str] = {"general": "system"}
        # Per-room encryption keys: room → derived AES key bytes.
        self._room_keys: dict[str, bytes] = {}
        # Peers IP visibility toggle.
        self._show_peer_ip = False
        # Full channel member list (from host peer_list broadcasts).
        self._channel_peers: dict[str, str] = {}  # node_id → username
        # Image support.
        self._image_refs: list = []  # prevent GC of PhotoImage objects
        self._max_image_size: int = 5 * 1024 * 1024  # 5 MB default
        # Whether non-host users can create rooms (controlled by host).
        self._allow_room_create = True

        self.root = tk.Tk()
        self._update_title()
        self.root.geometry("960x640")
        self.root.minsize(720, 420)
        self.root.configure(bg=BG_MID)
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        self._configure_styles()
        self._build_ui()
        self._register_callbacks()

        # Bring window to front.
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

        # Sash drag detection — pause refreshes while dragging.
        self._sash_dragging = False
        # Cache for peer list diff.
        self._last_peer_items: list[str] = []

        # Kick off periodic refreshes.
        self._schedule_peer_refresh()
        self._schedule_voice_refresh()
        self.root.after(100, self._schedule_mic_meter)

    # ── Helpers ─────────────────────────────────────────────────────

    def _set_wan_ip(self, text: str) -> None:
        self._wan_entry.configure(state="normal")
        self._wan_entry.delete(0, tk.END)
        self._wan_entry.insert(0, text)
        self._wan_entry.configure(state="readonly")

    def _update_title(self) -> None:
        role = "Host" if self.node.is_host else "Client"
        self.root.title(
            f"P2P Chat — {self.node.username} [{role}]"
        )

    # ── Styles ──────────────────────────────────────────────────────

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=BG_MID, foreground=FG_DEFAULT,
                         fieldbackground=BG_DARK, borderwidth=0)
        style.configure("TFrame", background=BG_MID)
        style.configure("TLabel", background=BG_MID, foreground=FG_DEFAULT,
                         font=(FONT_FAMILY, 10))
        style.configure("TButton", background=BG_LIGHT, foreground=FG_DEFAULT,
                         font=(FONT_FAMILY, 10), padding=(8, 4))
        style.map("TButton",
                  background=[("active", BG_DARK), ("disabled", BG_MID)],
                  foreground=[("disabled", FG_DIM)])
        style.configure("Accent.TButton", background="#2d5a88",
                         foreground="#ffffff")
        style.map("Accent.TButton",
                  background=[("active", "#3a7bc8"), ("disabled", BG_MID)])
        style.configure("Danger.TButton", background="#8b2020",
                         foreground="#ffffff")
        style.map("Danger.TButton",
                  background=[("active", "#b33030"), ("disabled", BG_MID)])
        style.configure("TEntry", fieldbackground=BG_DARK,
                         foreground=FG_DEFAULT, insertcolor=FG_DEFAULT)
        style.configure("TCombobox", fieldbackground=BG_DARK,
                         foreground=FG_DEFAULT, selectbackground=BG_LIGHT,
                         selectforeground=FG_DEFAULT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG_DARK)],
                  foreground=[("readonly", FG_DEFAULT)])
        self.root.option_add("*TCombobox*Listbox.background", BG_DARK)
        self.root.option_add("*TCombobox*Listbox.foreground", FG_DEFAULT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BG_LIGHT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG_DEFAULT)
        style.configure("TCheckbutton", background=BG_MID,
                         foreground=FG_DEFAULT, font=(FONT_FAMILY, 9))
        style.map("TCheckbutton",
                  background=[("active", BG_MID)],
                  foreground=[("active", FG_DEFAULT)])
        # Horizontal progress bars use layout "Horizontal.TProgressbar" — do not
        # invent "Mic.TProgressbar" without style.layout(...) or Tcl errors.
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=BG_DARK,
            background=FG_GREEN,
            borderwidth=0,
        )
        style.configure("Side.TLabelframe", background=BG_MID,
                         foreground=FG_ACCENT)
        style.configure("Side.TLabelframe.Label", background=BG_MID,
                         foreground=FG_ACCENT, font=(FONT_FAMILY, 10, "bold"))

    # ── Layout ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)
        def _on_sash_press(_):
            self._sash_dragging = True
            # Temporarily disable word-wrap to avoid expensive
            # re-layout of the Text widget during drag.
            self.chat_text.configure(wrap=tk.NONE)
        def _on_sash_release(_):
            self._sash_dragging = False
            self.chat_text.configure(wrap=tk.WORD)
        pane.bind("<Button-1>", _on_sash_press)
        pane.bind("<ButtonRelease-1>", _on_sash_release)

        # ── Left sidebar ────────────────────────────────────────────
        sidebar = ttk.Frame(pane, width=240)
        sidebar.pack_propagate(False)
        pane.add(sidebar, weight=0)

        # ── Username ────────────────────────────────────────────────
        uf = ttk.LabelFrame(sidebar, text="  Username  ",
                             style="Side.TLabelframe")
        uf.pack(fill=tk.X, padx=6, pady=(6, 3))
        self.username_label = ttk.Label(uf, text=self.node.username,
                                         foreground=FG_GREEN,
                                         font=(FONT_FAMILY, 11, "bold"))
        self.username_label.pack(padx=6, pady=6)

        # ── Rooms ───────────────────────────────────────────────────
        rf = ttk.LabelFrame(sidebar, text="  Rooms  ",
                             style="Side.TLabelframe")
        rf.pack(fill=tk.X, padx=6, pady=3)

        room_list_frame = ttk.Frame(rf)
        room_list_frame.pack(fill=tk.X, padx=2, pady=2)
        self.room_listbox = tk.Listbox(
            room_list_frame, bg=BG_DARK, fg=FG_DEFAULT,
            selectbackground=BG_LIGHT, selectforeground=FG_CYAN,
            font=(FONT_FAMILY, 10), relief=tk.FLAT, bd=4,
            highlightthickness=0, exportselection=False, height=5,
        )
        room_sb = ttk.Scrollbar(room_list_frame,
                                 command=self.room_listbox.yview)
        self.room_listbox.configure(yscrollcommand=room_sb.set)
        room_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.room_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.room_listbox.bind("<<ListboxSelect>>", self._on_room_select)

        room_add_frame = ttk.Frame(rf)
        room_add_frame.pack(fill=tk.X, padx=2, pady=(0, 2))
        self.room_entry = ttk.Entry(room_add_frame)
        self.room_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.room_entry.bind("<Return>", lambda _: self._on_room_add())
        ttk.Button(room_add_frame, text="+", width=3,
                   command=self._on_room_add).pack(side=tk.RIGHT)

        room_del_frame = ttk.Frame(rf)
        room_del_frame.pack(fill=tk.X, padx=2, pady=(0, 4))
        self.btn_room_delete = ttk.Button(
            room_del_frame, text="Delete Room", style="Danger.TButton",
            command=self._on_room_delete)
        self.btn_room_delete.pack(fill=tk.X)

        # ── Encryption key ──────────────────────────────────────────
        kf = ttk.LabelFrame(sidebar, text="  Room Key  ",
                             style="Side.TLabelframe")
        kf.pack(fill=tk.X, padx=6, pady=3)

        self.key_entry = ttk.Entry(kf, show="*")
        self.key_entry.pack(fill=tk.X, padx=4, pady=(4, 2))
        self.key_entry.bind("<Return>", lambda _: self._on_set_key())

        key_btn_row = ttk.Frame(kf)
        key_btn_row.pack(fill=tk.X, padx=4, pady=(0, 4))
        ttk.Button(key_btn_row, text="Set Key", style="Accent.TButton",
                   command=self._on_set_key).pack(side=tk.LEFT, expand=True,
                                                    fill=tk.X, padx=(0, 2))
        ttk.Button(key_btn_row, text="Clear", style="Danger.TButton",
                   command=self._on_clear_key).pack(side=tk.RIGHT, expand=True,
                                                      fill=tk.X, padx=(2, 0))

        self.key_status = ttk.Label(kf, text="No key", foreground=FG_DIM)
        self.key_status.pack(padx=4, pady=(0, 4))

        # ── Peers ───────────────────────────────────────────────────
        pf = ttk.LabelFrame(sidebar, text="  Peers  ",
                             style="Side.TLabelframe")
        pf.pack(fill=tk.X, padx=6, pady=3)

        peer_top = ttk.Frame(pf)
        peer_top.pack(fill=tk.X, padx=2, pady=(2, 0))
        self.btn_toggle_ip = ttk.Button(
            peer_top, text="Show IP", width=8,
            command=self._on_toggle_peer_ip)
        self.btn_toggle_ip.pack(side=tk.RIGHT)

        peer_list_frame = ttk.Frame(pf)
        peer_list_frame.pack(fill=tk.X, padx=2, pady=2)
        self.peer_listbox = tk.Listbox(
            peer_list_frame, bg=BG_DARK, fg=FG_DEFAULT,
            font=(FONT_FAMILY, 10), relief=tk.FLAT, bd=4,
            highlightthickness=0, height=4,
        )
        peer_sb = ttk.Scrollbar(peer_list_frame,
                                 command=self.peer_listbox.yview)
        self.peer_listbox.configure(yscrollcommand=peer_sb.set)
        peer_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.peer_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── Channel Info (host only) ────────────────────────────────
        if self.node.is_host:
            cf = ttk.LabelFrame(sidebar, text="  Channel Info  ",
                                 style="Side.TLabelframe")
            cf.pack(fill=tk.X, padx=6, pady=(3, 6))
            ttk.Label(cf, text=f"Port: {self.node.port}",
                      foreground=FG_GREEN).pack(padx=4, pady=4)

            local_ip = detect_lan_ip()

            ttk.Label(cf, text="Others join with:",
                      foreground=FG_DIM).pack(padx=4)
            lan_entry = ttk.Entry(cf)
            lan_entry.insert(0, f"{local_ip}:{self.node.port}")
            lan_entry.configure(state="readonly")
            lan_entry.pack(fill=tk.X, padx=4, pady=(2, 2))
            ttk.Label(cf, text="(LAN)", foreground=FG_DIM).pack(padx=4)

            self._wan_entry = ttk.Entry(cf)
            self._wan_entry.insert(0, "detecting...")
            self._wan_entry.configure(state="readonly")
            self._wan_entry.pack(fill=tk.X, padx=4, pady=(2, 2))
            ttk.Label(cf, text="(WAN)", foreground=FG_DIM).pack(padx=4)

            def _fetch_wan_ip():
                import urllib.request
                try:
                    with urllib.request.urlopen(
                        "https://api.ipify.org", timeout=5
                    ) as resp:
                        pub_ip = resp.read().decode().strip()
                except Exception:
                    pub_ip = "unavailable"
                if not self._closing:
                    self.root.after(0, self._set_wan_ip,
                                    f"{pub_ip}:{self.node.port}")

            threading.Thread(target=_fetch_wan_ip, daemon=True).start()

            self._allow_room_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(
                cf, text="Allow others to create rooms",
                variable=self._allow_room_var,
                command=self._on_toggle_allow_rooms,
            ).pack(anchor=tk.W, padx=4, pady=(2, 4))

            # Max image size control.
            img_row = ttk.Frame(cf)
            img_row.pack(fill=tk.X, padx=4, pady=(0, 6))
            ttk.Label(img_row, text="Max image (MB):",
                      foreground=FG_DIM).pack(side=tk.LEFT)
            self._max_img_var = tk.StringVar(value="5")
            ttk.Entry(img_row, textvariable=self._max_img_var,
                      width=5).pack(side=tk.LEFT, padx=(4, 4))
            ttk.Button(img_row, text="Set",
                       command=self._on_set_max_image_size
                       ).pack(side=tk.LEFT)

        # ── Right main area ─────────────────────────────────────────
        main = ttk.Frame(pane)
        pane.add(main, weight=1)

        # Chat display
        chat_frame = ttk.Frame(main)
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=(0, 6), pady=(6, 0))

        self.chat_text = tk.Text(
            chat_frame, bg=BG_DARK, fg=FG_DEFAULT, font=(FONT_FAMILY, 11),
            wrap=tk.WORD, relief=tk.FLAT, bd=8, highlightthickness=0,
            state=tk.DISABLED, cursor="arrow",
        )
        scrollbar = ttk.Scrollbar(chat_frame, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.chat_text.tag_configure("username", foreground=FG_GREEN,
                                      font=(FONT_FAMILY, 11, "bold"))
        self.chat_text.tag_configure("timestamp", foreground=FG_DIM)
        self.chat_text.tag_configure("system", foreground=FG_YELLOW)
        self.chat_text.tag_configure("voice", foreground=FG_MAGENTA)
        self.chat_text.tag_configure("error", foreground=FG_RED)
        self.chat_text.tag_configure("encrypted", foreground=FG_RED,
                                      font=(FONT_FAMILY, 11, "italic"))
        self.chat_text.tag_configure("self_user", foreground=FG_CYAN,
                                      font=(FONT_FAMILY, 11, "bold"))

        # ── Voice user list panel ───────────────────────────────────
        self.voice_panel = ttk.LabelFrame(
            main, text="  Voice  ", style="Side.TLabelframe")
        self.voice_panel.pack(fill=tk.X, padx=(0, 6), pady=(4, 0))

        # Voice user list (top, grows/shrinks dynamically).
        self._voice_user_frame = ttk.Frame(self.voice_panel)
        self._voice_user_frame.pack(fill=tk.X, padx=4, pady=(4, 0))
        self._voice_rows: dict[str, dict[str, Any]] = {}

        # Voice controls row (keep ref for pack ordering).
        self._voice_ctrl = ttk.Frame(self.voice_panel)
        self._voice_ctrl.pack(fill=tk.X, padx=4, pady=(4, 0))

        self.voice_label = ttk.Label(self._voice_ctrl, text="Voice: inactive",
                                      foreground=FG_DIM)
        self.voice_label.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_voice_join = ttk.Button(
            self._voice_ctrl, text="Join Voice", style="Accent.TButton",
            command=self._on_voice_join)
        self.btn_voice_join.pack(side=tk.LEFT, padx=2)

        self.btn_voice_leave = ttk.Button(
            self._voice_ctrl, text="Leave", style="Danger.TButton",
            command=self._on_voice_leave, state=tk.DISABLED)
        self.btn_voice_leave.pack(side=tk.LEFT, padx=2)

        self.btn_voice_mute = ttk.Button(
            self._voice_ctrl, text="Mute", command=self._on_voice_mute,
            state=tk.DISABLED)
        self.btn_voice_mute.pack(side=tk.LEFT, padx=2)

        # Local mic feedback (your voice only — helps tell "me vs them").
        self._mic_fb_row = ttk.Frame(self.voice_panel)
        self._mic_fb_row.pack(fill=tk.X, padx=4, pady=(0, 4))
        ttk.Label(
            self._mic_fb_row, text="本机麦", foreground=FG_DIM,
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(0, 6))
        self._mic_status = ttk.Label(
            self._mic_fb_row,
            text="未加入语音",
            font=(FONT_FAMILY, 9),
            foreground=FG_DIM,
        )
        self._mic_status.pack(side=tk.LEFT, padx=(0, 8))
        self._mic_meter = ttk.Progressbar(
            self._mic_fb_row,
            length=160,
            maximum=100,
            mode="determinate",
            orient=tk.HORIZONTAL,
        )
        self._mic_meter.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Audio device selection row
        dev_frame = ttk.Frame(self.voice_panel)
        dev_frame.pack(fill=tk.X, padx=4, pady=(0, 4))

        ttk.Label(dev_frame, text="Mic:", foreground=FG_DIM).pack(
            side=tk.LEFT, padx=(0, 4))
        self._input_devices = list_input_devices()
        input_names = ["Default"] + [d[1] for d in self._input_devices]
        self.cmb_input = ttk.Combobox(
            dev_frame, values=input_names, state="readonly",
            width=18, font=(FONT_FAMILY, 9))
        self.cmb_input.current(0)
        self.cmb_input.pack(side=tk.LEFT, padx=(0, 8))
        self.cmb_input.bind("<<ComboboxSelected>>", self._on_input_device_change)

        ttk.Label(dev_frame, text="Speaker:", foreground=FG_DIM).pack(
            side=tk.LEFT, padx=(0, 4))
        self._output_devices = list_output_devices()
        output_names = ["Default"] + [d[1] for d in self._output_devices]
        self.cmb_output = ttk.Combobox(
            dev_frame, values=output_names, state="readonly",
            width=18, font=(FONT_FAMILY, 9))
        self.cmb_output.current(0)
        self.cmb_output.pack(side=tk.LEFT)
        self.cmb_output.bind("<<ComboboxSelected>>", self._on_output_device_change)

        # ── Input bar (fixed height) ────────────────────────────────
        input_frame = ttk.Frame(main, height=36)
        input_frame.pack(fill=tk.X, padx=(0, 6), pady=(4, 6))
        input_frame.pack_propagate(False)

        self.btn_attach = ttk.Button(
            input_frame, text="+", width=3, command=self._on_attach_menu)
        self.btn_attach.pack(side=tk.LEFT, padx=(4, 0))

        self.msg_entry = ttk.Entry(input_frame, font=(FONT_FAMILY, 11))
        self.msg_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        self.msg_entry.bind("<Return>", lambda _: self._on_send())
        self.msg_entry.focus_set()

        ttk.Button(input_frame, text="Send", style="Accent.TButton",
                   command=self._on_send).pack(side=tk.RIGHT)

        # Populate initial room list.
        self._refresh_room_list()

    # ── Callback registration ───────────────────────────────────────

    def _register_callbacks(self) -> None:
        self.node.on_message = self._cb_message
        self.node.on_voice_signal = self._cb_voice_signal
        self.node.on_peer_disconnect = self._cb_peer_disconnect
        self.node.on_system_message = self._cb_system
        self.node.on_peer_join = self._cb_peer_join
        self.voice.on_mute_change = self._on_mute_change

    # Thread-safe bridges: called on asyncio thread → post to tkinter.

    def _cb_message(self, msg: dict) -> None:
        if not self._closing:
            self.root.after(0, self._display_message, msg)

    def _cb_voice_signal(self, msg: dict) -> None:
        if not self._closing:
            self.root.after(0, self._handle_voice_signal, msg)

    def _cb_peer_disconnect(self, node_id: str) -> None:
        self.voice.handle_peer_disconnect(node_id)
        if not self._closing:
            self.root.after(0, self._refresh_peers)

    def _cb_system(self, text: str) -> None:
        if not self._closing:
            self.root.after(0, self._append_system, text)

    def _cb_peer_join(self, peer) -> None:
        """Send channel config, rooms, and voice state to a new client."""
        # Send current permission setting.
        cfg = channel_config(self.node.node_id, self._allow_room_create,
                             max_image_size=self._max_image_size)
        self._run_async(peer.send(cfg))
        # Send existing rooms.
        for room_name in self._rooms:
            if room_name == "general":
                continue
            owner = self._room_owner.get(room_name, self.node.node_id)
            msg = room_create(owner, self.node.username, room_name)
            self._run_async(peer.send(msg))
        # Send current voice participants so the new client knows who's
        # in voice and (critically) knows the host's UDP address.
        from .protocol import voice_join as _voice_join
        for nid, uname in self.voice.voice_members.items():
            room = self.voice.active_room or "general"
            if nid == self.node.node_id:
                # Host entry: send real UDP address so client can send to us.
                msg = _voice_join(
                    nid, uname, room,
                    self.voice.public_host, self.voice.udp_port)
            else:
                # Other clients: display-only, no UDP address needed
                # (clients only talk to the host, not to each other).
                msg = _voice_join(nid, uname, room, "", 0)
            self._run_async(peer.send(msg))

    # ── Generic helpers ─────────────────────────────────────────────

    def _run_async(self, coro) -> None:
        """Schedule a coroutine on the asyncio loop from the GUI thread."""
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _append_chat(self, text: str, tag: str = "") -> None:
        """Append a line to the chat Text widget."""
        self.chat_text.configure(state=tk.NORMAL)
        if tag:
            self.chat_text.insert(tk.END, text, tag)
        else:
            self.chat_text.insert(tk.END, text)
        self.chat_text.configure(state=tk.DISABLED)
        self.chat_text.see(tk.END)

    def _append_formatted_message(
        self, username: str, text: str, ts: str, *,
        is_self: bool = False, is_encrypted_placeholder: bool = False,
    ) -> None:
        """Append a fully formatted chat message with coloured parts."""
        user_tag = "self_user" if is_self else "username"
        msg_tag = "encrypted" if is_encrypted_placeholder else ""
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.insert(tk.END, f"[{ts}] ", "timestamp")
        self.chat_text.insert(tk.END, username, user_tag)
        if msg_tag:
            self.chat_text.insert(tk.END, f": {text}\n", msg_tag)
        else:
            self.chat_text.insert(tk.END, f": {text}\n")
        self.chat_text.configure(state=tk.DISABLED)
        self.chat_text.see(tk.END)

    def _append_system(self, text: str) -> None:
        self._append_chat(f"[system] {text}\n", "system")
        self._store_history("system", f"[system] {text}")

    def _store_history(self, tag: str, text: str) -> None:
        """Save a line to the current room's history."""
        self._room_history.setdefault(self.room, []).append((tag, text))

    # ── Encryption helpers ──────────────────────────────────────────

    def _encrypt_text(self, plaintext: str, room: str) -> str | None:
        """Encrypt if the room has a key.  Returns ciphertext or None."""
        key = self._room_keys.get(room)
        if key is None:
            return None
        return encrypt(plaintext, key)

    def _decrypt_msg(self, msg: dict) -> str:
        """Try to decrypt a message; return plaintext or a placeholder."""
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

    # ── Display handlers (run on tkinter thread) ────────────────────

    def _display_message(self, msg: dict) -> None:
        mtype = msg.get("type")

        # ── Peer list from host ─────────────────────────────────────
        if mtype == "peer_list":
            self._channel_peers.clear()
            for p in msg.get("peers", []):
                nid = p.get("node_id", "")
                uname = p.get("username", nid)
                if nid:
                    self._channel_peers[nid] = uname
            self._refresh_peers()
            return

        # ── Channel config from host ────────────────────────────────
        if mtype == "channel_config":
            self._allow_room_create = msg.get("allow_room_create", True)
            if "max_image_size" in msg:
                self._max_image_size = msg["max_image_size"]
            state = "enabled" if self._allow_room_create else "disabled"
            self._append_system(f"Host has {state} room creation.")
            return

        # ── Room lifecycle messages ─────────────────────────────────
        if mtype == "room_create":
            room = msg.get("room", "")
            creator = msg.get("from", "")
            username = msg.get("username", creator)
            if room and room not in self._rooms:
                self._rooms.add(room)
                self._room_owner[room] = creator
                self._refresh_room_list()
                self._append_system(f"{username} created room #{room}")
            return

        if mtype == "room_delete":
            room = msg.get("room", "")
            username = msg.get("username", msg.get("from", "?"))
            if room in self._rooms and room != "general":
                self._rooms.discard(room)
                self._room_owner.pop(room, None)
                self._room_keys.pop(room, None)
                self._room_history.pop(room, None)
                if self.room == room:
                    self.room = "general"
                self._refresh_room_list()
                self._reload_chat_for_room()
                self._update_key_status()
                self._append_system(f"{username} deleted room #{room}")
            return

        if mtype == "image":
            self._handle_image_message(msg)
            return

        if mtype != "chat":
            return
        room = msg.get("room", "")
        if room and room not in self._rooms:
            self._rooms.add(room)
            self._refresh_room_list()

        username = msg.get("username", msg.get("from", "?"))
        ts = msg.get("ts", "")
        text = self._decrypt_msg(msg)
        is_placeholder = text.startswith("[encrypted")

        # Store in history for that room.
        line = f"[{ts}] {username}: {text}"
        tag = "encrypted" if is_placeholder else "chat"
        self._room_history.setdefault(room, []).append((tag, line))

        # Only display if it matches the active room.
        if room != self.room:
            return
        self._append_formatted_message(
            username, text, ts, is_encrypted_placeholder=is_placeholder
        )

    def _handle_voice_signal(self, msg: dict) -> None:
        # Replace self-reported udp_host with the real IP from the TCP
        # connection.  The self-reported address is a LAN IP that remote
        # peers can't reach; the TCP socket's real_ip is the actual
        # routable address (public IP through NAT).
        sender = msg.get("from", "")
        if sender in self.node.peers:
            peer = self.node.peers[sender]
            if peer.real_ip:
                msg = dict(msg, udp_host=peer.real_ip)
        self.voice.handle_voice_signal(msg)
        mtype = msg.get("type")
        username = msg.get("username", msg.get("from", "?"))
        room = msg.get("room", "")
        if room == self.room:
            if mtype == "voice_join":
                self._append_chat(
                    f"[voice] {username} joined voice\n", "voice")
            elif mtype == "voice_leave":
                self._append_chat(
                    f"[voice] {username} left voice\n", "voice")
            elif mtype == "voice_force_mute":
                target = msg.get("target", "")
                forced = msg.get("muted", True)
                if target == self.node.node_id:
                    state = "muted you" if forced else "unmuted you"
                    self._append_chat(
                        f"[voice] Host {state}\n", "voice")
        self._update_voice_ui()

    # ── User actions ────────────────────────────────────────────────

    def _on_send(self) -> None:
        text = self.msg_entry.get().strip()
        if not text:
            return
        self.msg_entry.delete(0, tk.END)

        encrypted = self._encrypt_text(text, self.room)
        msg = chat_message(
            self.node.node_id, self.room, text, self.node.username,
            encrypted_msg=encrypted,
        )


        # Display locally (always show plaintext for our own messages).
        ts = msg["ts"]
        self._append_formatted_message(self.node.username, text, ts,
                                        is_self=True)
        line = f"[{ts}] {self.node.username}: {text}"
        self._room_history.setdefault(self.room, []).append(("self", line))

        self._run_async(self.node.broadcast(msg))

    # ── Image sending ───────────────────────────────────────────

    def _on_attach_menu(self) -> None:
        menu = tk.Menu(self.root, tearoff=0, bg=BG_DARK, fg=FG_DEFAULT,
                       activebackground=BG_LIGHT, activeforeground=FG_DEFAULT,
                       font=(FONT_FAMILY, 10))
        menu.add_command(label="Image", command=self._on_send_image)
        x = self.btn_attach.winfo_rootx()
        y = self.btn_attach.winfo_rooty()
        menu.tk_popup(x, y - 40)

    def _on_send_image(self) -> None:
        if Image is None:
            self._append_system("Pillow not installed. pip install Pillow")
            return
        filetypes = [
            ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
            ("All files", "*.*"),
        ]
        path = filedialog.askopenfilename(
            title="Select Image", filetypes=filetypes, parent=self.root)
        if not path:
            return

        file_size = os.path.getsize(path)
        if file_size > self._max_image_size:
            limit_mb = self._max_image_size / (1024 * 1024)
            self._append_system(
                f"Image too large ({file_size / (1024*1024):.1f} MB). "
                f"Max: {limit_mb:.1f} MB.")
            return

        with open(path, "rb") as f:
            raw = f.read()

        filename = os.path.basename(path)
        b64 = base64.b64encode(raw).decode("ascii")

        encrypted = None
        key = self._room_keys.get(self.room)
        if key:
            encrypted = base64.b64encode(
                encrypt_bytes(raw, key)).decode("ascii")

        msg = image_message(
            self.node.node_id, self.room, self.node.username,
            filename, b64, encrypted_image=encrypted)

        ts = msg["ts"]
        self._display_image_inline(
            self.node.username, filename, raw, ts, is_self=True)
        self._room_history.setdefault(self.room, []).append(
            ("image_self", (self.node.username, filename, raw, ts)))

        self._run_async(self.node.broadcast(msg))

    def _handle_image_message(self, msg: dict) -> None:
        """Process an inbound image message."""
        room = msg.get("room", "")
        if room and room not in self._rooms:
            self._rooms.add(room)
            self._refresh_room_list()

        username = msg.get("username", msg.get("from", "?"))
        ts = msg.get("ts", "")
        filename = msg.get("filename", "image")

        encrypted = msg.get("encrypted_image")
        if encrypted:
            key = self._room_keys.get(room)
            if key is None:
                placeholder = f"sent an image [encrypted - no key]"
                self._room_history.setdefault(room, []).append(
                    ("system", f"[{ts}] {username} {placeholder}"))
                if room == self.room:
                    self._append_formatted_message(
                        username, placeholder, ts,
                        is_encrypted_placeholder=True)
                return
            raw = decrypt_bytes(base64.b64decode(encrypted), key)
            if raw is None:
                placeholder = f"sent an image [encrypted - wrong key]"
                self._room_history.setdefault(room, []).append(
                    ("system", f"[{ts}] {username} {placeholder}"))
                if room == self.room:
                    self._append_formatted_message(
                        username, placeholder, ts,
                        is_encrypted_placeholder=True)
                return
        else:
            raw = base64.b64decode(msg.get("image_data", ""))

        self._room_history.setdefault(room, []).append(
            ("image", (username, filename, raw, ts)))
        if room == self.room:
            self._display_image_inline(username, filename, raw, ts)

    def _display_image_inline(
        self, username: str, filename: str, raw: bytes,
        ts: str, *, is_self: bool = False,
    ) -> None:
        """Show an image thumbnail inline in chat."""
        if Image is None:
            self._append_chat(f"[{ts}] {username} sent {filename}\n")
            return

        user_tag = "self_user" if is_self else "username"
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.insert(tk.END, f"[{ts}] ", "timestamp")
        self.chat_text.insert(tk.END, username, user_tag)
        self.chat_text.insert(tk.END, f" sent {filename}:\n")

        try:
            img = Image.open(BytesIO(raw))
            max_w = 300
            if img.width > max_w:
                ratio = max_w / img.width
                img = img.resize(
                    (max_w, int(img.height * ratio)), Image.LANCZOS)
            tk_img = ImageTk.PhotoImage(img)
            self._image_refs.append(tk_img)
            self.chat_text.image_create(tk.END, image=tk_img)

            # Click to open full size.
            tag = f"img_{len(self._image_refs)}"
            self.chat_text.tag_add(tag, "end-2c", "end-1c")
            self.chat_text.tag_bind(
                tag, "<Button-1>",
                lambda e, r=raw, f=filename: self._open_full_image(r, f))
            self.chat_text.tag_bind(
                tag, "<Enter>",
                lambda e: self.chat_text.configure(cursor="hand2"))
            self.chat_text.tag_bind(
                tag, "<Leave>",
                lambda e: self.chat_text.configure(cursor="arrow"))
        except Exception:
            self.chat_text.insert(tk.END, "[failed to load image]", "error")

        self.chat_text.insert(tk.END, "\n")
        self.chat_text.configure(state=tk.DISABLED)
        self.chat_text.see(tk.END)

    def _open_full_image(self, raw: bytes, filename: str) -> None:
        """Open full-size image in a new window."""
        if Image is None:
            return
        win = tk.Toplevel(self.root)
        win.title(f"Image — {filename}")
        win.configure(bg=BG_DARK)
        img = Image.open(BytesIO(raw))
        max_w = win.winfo_screenwidth() - 100
        max_h = win.winfo_screenheight() - 100
        if img.width > max_w or img.height > max_h:
            img.thumbnail((max_w, max_h), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(img)
        lbl = tk.Label(win, image=tk_img, bg=BG_DARK)
        lbl.image = tk_img
        lbl.pack(fill=tk.BOTH, expand=True)

    def _on_set_max_image_size(self) -> None:
        try:
            mb = float(self._max_img_var.get())
            if mb <= 0:
                raise ValueError
        except ValueError:
            self._append_system("Invalid image size limit.")
            return
        self._max_image_size = int(mb * 1024 * 1024)
        msg = channel_config(
            self.node.node_id, self._allow_room_create,
            max_image_size=self._max_image_size)
        self._run_async(self.node.broadcast(msg))
        self._append_system(f"Max image size set to {mb:.1f} MB")

    # ── Peers IP toggle ───────────────────────────────────────────

    def _on_toggle_peer_ip(self) -> None:
        self._show_peer_ip = not self._show_peer_ip
        self.btn_toggle_ip.configure(
            text="Hide IP" if self._show_peer_ip else "Show IP")
        self._refresh_peers()

    # ── Host: allow/disallow room creation ──────────────────────────

    def _on_toggle_allow_rooms(self) -> None:
        self._allow_room_create = self._allow_room_var.get()
        msg = channel_config(self.node.node_id, self._allow_room_create,
                             max_image_size=self._max_image_size)
        self._run_async(self.node.broadcast(msg))
        state = "enabled" if self._allow_room_create else "disabled"
        self._append_system(f"Room creation by others: {state}")

    # ── Room key management ─────────────────────────────────────────

    def _on_set_key(self) -> None:
        passphrase = self.key_entry.get()
        if not passphrase:
            self._append_system("Enter a passphrase to set the room key.")
            return
        self._room_keys[self.room] = derive_key(passphrase, self.room)
        self.key_entry.delete(0, tk.END)
        self._update_key_status()
        self._sync_voice_key()
        self._append_system(
            f"Encryption key set for #{self.room}. "
            "Text and voice will be encrypted with AES-256-GCM."
        )

    def _on_clear_key(self) -> None:
        if self.room in self._room_keys:
            del self._room_keys[self.room]
        self.key_entry.delete(0, tk.END)
        self._update_key_status()
        self._sync_voice_key()
        self._append_system(f"Encryption key cleared for #{self.room}.")

    def _sync_voice_key(self) -> None:
        """Keep VoiceEngine's room_key in sync with the active room's key."""
        if self.voice.active_room:
            self.voice.room_key = self._room_keys.get(
                self.voice.active_room)

    def _update_key_status(self) -> None:
        if self.room in self._room_keys:
            self.key_status.configure(text="Encrypted", foreground=FG_GREEN)
        else:
            self.key_status.configure(text="No key", foreground=FG_DIM)

    # ── Room selection ──────────────────────────────────────────────

    def _on_room_select(self, _event: Any = None) -> None:
        sel = self.room_listbox.curselection()
        if not sel:
            return
        new_room = self.room_listbox.get(sel[0])
        if new_room == self.room:
            return
        self.room = new_room
        self._update_key_status()
        self._reload_chat_for_room()

    def _on_room_add(self) -> None:
        name = self.room_entry.get().strip()
        if not name:
            return
        if name in self._rooms:
            self._append_system(f"Room #{name} already exists.")
            return
        if not self.node.is_host and not self._allow_room_create:
            self._append_system("Host has disabled room creation.")
            return
        self.room_entry.delete(0, tk.END)

        # Register locally as my room.
        self._rooms.add(name)
        self._room_owner[name] = self.node.node_id
        self._refresh_room_list()
        self.room = name
        self._select_room_in_list(name)
        self._update_key_status()
        self._reload_chat_for_room()
        self._append_system(f"Room #{name} created.")

        # Broadcast to everyone.
        msg = room_create(self.node.node_id, self.node.username, name)
        self._run_async(self.node.broadcast(msg))

    def _on_room_delete(self) -> None:
        sel = self.room_listbox.curselection()
        if not sel:
            self._append_system("Select a room to delete.")
            return
        name = self.room_listbox.get(sel[0])

        if name == "general":
            self._append_system("Cannot delete the default room.")
            return

        owner = self._room_owner.get(name)
        is_mine = owner == self.node.node_id
        if not is_mine and not self.node.is_host:
            self._append_system(
                f"Cannot delete #{name} — only the creator or host can delete it.")
            return

        # Broadcast deletion.
        msg = room_delete(self.node.node_id, self.node.username, name)
        self._run_async(self.node.broadcast(msg))

        # Remove locally.
        self._rooms.discard(name)
        self._room_owner.pop(name, None)
        self._room_keys.pop(name, None)
        self._room_history.pop(name, None)

        if self.room == name:
            self.room = "general"
        self._refresh_room_list()
        self._reload_chat_for_room()
        self._update_key_status()
        self._append_system(f"Room #{name} deleted.")

    # ── Voice controls ──────────────────────────────────────────────

    def _on_voice_join(self) -> None:
        if not self.voice.available:
            self._append_system("Voice unavailable — pip install pyaudio")
            return
        if self.voice.active_room:
            self._append_system(
                f"Already in voice for #{self.voice.active_room}")
            return

        async def _join():
            self.voice.room_key = self._room_keys.get(self.room)
            msg = await self.voice.join(self.room)
            await self.node.broadcast(msg)
            if not self._closing:
                self.root.after(0, self._voice_joined)

        self._run_async(_join())

    def _voice_joined(self) -> None:
        self._append_chat(
            f"[voice] Joined voice in #{self.room}\n", "voice")
        self._update_voice_ui()

    def _on_voice_leave(self) -> None:
        if not self.voice.active_room:
            return

        async def _leave():
            msg = await self.voice.leave()
    
            await self.node.broadcast(msg)
            if not self._closing:
                self.root.after(0, self._voice_left)

        self._run_async(_leave())

    def _voice_left(self) -> None:
        self._append_chat("[voice] Left voice channel\n", "voice")
        self._update_voice_ui()

    def _on_voice_mute(self) -> None:
        if self.voice.muted:
            self.voice.unmute()
        else:
            self.voice.mute()
        self._update_voice_ui()

    def _on_input_device_change(self, _event: Any = None) -> None:
        idx = self.cmb_input.current()
        if idx == 0:
            self.voice.input_device = None
        else:
            self.voice.input_device = self._input_devices[idx - 1][0]

    def _on_output_device_change(self, _event: Any = None) -> None:
        idx = self.cmb_output.current()
        if idx == 0:
            self.voice.output_device = None
        else:
            self.voice.output_device = self._output_devices[idx - 1][0]

    def _update_voice_ui(self) -> None:
        active = self.voice.active_room is not None
        if active:
            room = self.voice.active_room
            if self.voice.force_muted:
                self.voice_label.configure(
                    text=f"Voice: #{room} (server muted)", foreground=FG_RED)
            elif self.voice.muted:
                self.voice_label.configure(
                    text=f"Voice: #{room} (muted)", foreground=FG_RED)
            else:
                self.voice_label.configure(
                    text=f"Voice: #{room}", foreground=FG_GREEN)
            self.btn_voice_join.configure(state=tk.DISABLED)
            self.btn_voice_leave.configure(state=tk.NORMAL)
            mute_state = tk.DISABLED if self.voice.force_muted else tk.NORMAL
            self.btn_voice_mute.configure(
                state=mute_state,
                text="Unmute" if self.voice.muted else "Mute")
            self.cmb_input.configure(state=tk.DISABLED)
            self.cmb_output.configure(state=tk.DISABLED)
        else:
            self.voice_label.configure(
                text="Voice: inactive", foreground=FG_DIM)
            self.btn_voice_join.configure(state=tk.NORMAL)
            self.btn_voice_leave.configure(state=tk.DISABLED)
            self.btn_voice_mute.configure(state=tk.DISABLED, text="Mute")
            self.cmb_input.configure(state="readonly")
            self.cmb_output.configure(state="readonly")
        self._refresh_voice_users()

    def _refresh_voice_users(self) -> None:
        """Update the voice user list without destroying/recreating widgets."""
        # Use voice_members (all participants) for the display list.
        members = dict(self.voice.voice_members)

        wanted: list[tuple[str, str, bool, bool, bool, bool | None]] = []
        if self.voice.active_room:
            self_enc = self.voice.room_key is not None
            wanted.append((
                self.node.node_id, self.node.username,
                self.voice.muted, self.voice.is_self_speaking(),
                True, True if self_enc else None,
            ))
        for nid, uname in members.items():
            if nid == self.node.node_id:
                continue
            wanted.append((
                nid, uname,
                self.voice.is_peer_muted(nid),
                self.voice.is_peer_speaking(nid),
                False,
                self.voice.is_peer_encrypted(nid),
            ))

        wanted_ids = {w[0] for w in wanted}

        # Remove rows for users no longer in voice.
        for nid in list(self._voice_rows):
            if nid not in wanted_ids:
                self._voice_rows[nid]["row"].destroy()
                del self._voice_rows[nid]

        # Add or update rows.
        for node_id, username, muted, speaking, is_self, enc in wanted:
            if node_id in self._voice_rows:
                self._update_voice_row(
                    node_id, muted, speaking, enc)
            else:
                self._create_voice_row(
                    node_id, username, muted, speaking, is_self, enc)

        # Show/hide the user list frame so the panel shrinks when empty.
        if wanted:
            self._voice_user_frame.pack(
                fill=tk.X, padx=4, pady=4, before=self._voice_ctrl)
        else:
            self._voice_user_frame.pack_forget()

    def _create_voice_row(
        self, node_id: str, username: str,
        muted: bool, speaking: bool, is_self: bool,
        enc: bool | None,
    ) -> None:
        """Create a new voice user row and cache widget refs."""
        row = ttk.Frame(self._voice_user_frame)
        row.pack(fill=tk.X, pady=1)

        dot = tk.Label(row, text="\u25CF", bg=BG_MID,
                        font=(FONT_FAMILY, 10))
        dot.pack(side=tk.LEFT, padx=(2, 4))

        name_color = FG_CYAN if is_self else FG_DEFAULT
        name_lbl = ttk.Label(row, text=username, foreground=name_color,
                              font=(FONT_FAMILY, 10))
        name_lbl.pack(side=tk.LEFT)

        mic_lbl = ttk.Label(row, foreground=FG_DIM)
        mic_lbl.pack(side=tk.LEFT, padx=(6, 0))

        enc_lbl = ttk.Label(row, font=(FONT_FAMILY, 8))
        enc_lbl.pack(side=tk.LEFT, padx=(6, 0))

        btn = None
        if self.node.is_host and not is_self:
            btn = ttk.Button(row, width=6,
                             command=lambda nid=node_id: self._on_force_mute(
                                 nid, not self.voice.is_peer_muted(nid)))
            btn.pack(side=tk.RIGHT, padx=2)

        self._voice_rows[node_id] = {
            "row": row, "dot": dot, "mic": mic_lbl,
            "enc": enc_lbl, "btn": btn,
        }
        self._update_voice_row(node_id, muted, speaking, enc)

    def _update_voice_row(
        self, node_id: str, muted: bool, speaking: bool,
        enc: bool | None,
    ) -> None:
        """Update an existing voice row's dynamic properties (no flicker)."""
        refs = self._voice_rows.get(node_id)
        if not refs:
            return

        # Dot colour.
        if muted:
            dot_color = FG_RED
        elif speaking:
            dot_color = FG_GREEN
        else:
            dot_color = FG_DIM
        refs["dot"].configure(fg=dot_color)

        # Mic icon.
        refs["mic"].configure(text="\U0001F507" if muted else "\U0001F3A4")

        # Encryption badge.
        if enc is True:
            refs["enc"].configure(text="Encrypted", foreground=FG_GREEN)
        elif enc is False:
            refs["enc"].configure(text="No Key", foreground=FG_RED)
        else:
            refs["enc"].configure(text="")

        # Host mute button.
        if refs["btn"] is not None:
            refs["btn"].configure(text="Unmute" if muted else "Mute")

    def _on_force_mute(self, target_node_id: str, muted: bool) -> None:
        """Host forces a peer to mute/unmute."""
        room = self.voice.active_room or self.room
        msg = voice_force_mute(
            self.node.node_id, room, target_node_id, muted)
        self._run_async(self.node.broadcast(msg))
        # Also update local tracking so UI reflects immediately.
        self.voice._peer_muted[target_node_id] = muted
        self._refresh_voice_users()

    def _on_mute_change(self, muted: bool) -> None:
        """Called by VoiceEngine when local mute state changes."""
        room = self.voice.active_room
        if not room:
            return
        msg = voice_mute_status(
            self.node.node_id, self.node.username, room, muted)
        self._run_async(self.node.broadcast(msg))
        if not self._closing:
            self.root.after(0, self._update_voice_ui)

    # ── Room / peer list refresh ────────────────────────────────────

    def _refresh_room_list(self) -> None:
        self.room_listbox.delete(0, tk.END)
        for r in sorted(self._rooms):
            self.room_listbox.insert(tk.END, r)
        self._select_room_in_list(self.room)

    def _select_room_in_list(self, name: str) -> None:
        items = self.room_listbox.get(0, tk.END)
        for i, item in enumerate(items):
            if item == name:
                self.room_listbox.selection_clear(0, tk.END)
                self.room_listbox.selection_set(i)
                self.room_listbox.see(i)
                break

    def _reload_chat_for_room(self) -> None:
        """Clear the chat widget and replay history for the current room."""
        self.chat_text.configure(state=tk.NORMAL)
        self.chat_text.delete("1.0", tk.END)
        self.chat_text.configure(state=tk.DISABLED)

        history = self._room_history.get(self.room, [])
        for tag, line in history:
            if tag in ("image", "image_self"):
                username, filename, raw, ts = line
                self._display_image_inline(
                    username, filename, raw, ts,
                    is_self=(tag == "image_self"))
            elif tag == "system":
                self._append_chat(line + "\n", "system")
            elif tag == "voice":
                self._append_chat(line + "\n", "voice")
            elif tag == "self":
                self._append_chat(line + "\n", "self_user")
            elif tag == "encrypted":
                self._append_chat(line + "\n", "encrypted")
            else:
                self._append_chat(line + "\n")
        self._select_room_in_list(self.room)

    def _refresh_peers(self) -> None:
        """Only update the peer listbox when content actually changed."""
        items = []
        # Clients use the full member list from host; host uses local peers.
        if self._channel_peers and not self.node.is_host:
            for nid, uname in self._channel_peers.items():
                if nid == self.node.node_id:
                    continue  # don't show self
                items.append(uname)
        else:
            for pid, peer in self.node.peers.items():
                name = peer.remote_username or pid
                if self._show_peer_ip:
                    ip = peer.real_ip or "?"
                    port = peer.real_port or "?"
                    items.append(f"{name}  ({ip}:{port})")
                else:
                    items.append(name)
        if items == self._last_peer_items:
            return  # nothing changed — skip listbox rebuild
        self._last_peer_items = items
        self.peer_listbox.delete(0, tk.END)
        for item in items:
            self.peer_listbox.insert(tk.END, item)

    def _schedule_peer_refresh(self) -> None:
        """Refresh peer list every 2 seconds."""
        if self._closing:
            return
        if not self._sash_dragging:
            self._refresh_peers()
        self.root.after(2000, self._schedule_peer_refresh)

    def _schedule_voice_refresh(self) -> None:
        """Refresh voice user status every 500ms."""
        if self._closing:
            return
        if not self._sash_dragging:
            self._refresh_voice_users()
        self.root.after(500, self._schedule_voice_refresh)

    def _schedule_mic_meter(self) -> None:
        """Local mic level + speech detection (~10 Hz), independent of UDP."""
        if self._closing:
            return
        if self.voice.active_room:
            if self.voice.muted or self.voice.force_muted:
                self._mic_meter.configure(value=0)
                self._mic_status.configure(
                    text="已静音（本机不发送）",
                    foreground=FG_RED,
                )
            else:
                pct = int(min(100, max(0, self.voice.mic_input_level * 100)))
                self._mic_meter.configure(value=pct)
                if self.voice.is_self_speaking():
                    self._mic_status.configure(
                        text="检测到说话 — 本机正在发送语音",
                        foreground=FG_GREEN,
                    )
                else:
                    self._mic_status.configure(
                        text="未检测到明显人声（安静或音量偏低）",
                        foreground=FG_DIM,
                    )
        else:
            self._mic_meter.configure(value=0)
            self._mic_status.configure(
                text="未加入语音",
                foreground=FG_DIM,
            )
        self.root.after(100, self._schedule_mic_meter)

    # ── Shutdown ────────────────────────────────────────────────────

    def _on_closing(self) -> None:
        self._closing = True

        async def _shutdown():
            await self.voice.stop()
            await self.node.stop()

        future = asyncio.run_coroutine_threadsafe(_shutdown(), self.loop)
        try:
            future.result(timeout=_ASYNC_SHUTDOWN_TIMEOUT)
            self._async_shutdown_ok = True
        except concurrent.futures.TimeoutError:
            self._async_shutdown_ok = False
        except Exception:
            self._async_shutdown_ok = False

        self.root.destroy()

    # ── Entry point ─────────────────────────────────────────────────

    def run(self) -> None:
        """Block on the tkinter main loop."""
        self.root.mainloop()


# ═══════════════════════════════════════════════════════════════════
#  Startup dialog — "Create Channel" or "Join Channel".
# ═══════════════════════════════════════════════════════════════════

def _apply_dialog_styles(root: tk.Tk) -> None:
    """Shared dark-theme styles for dialog windows."""
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG_MID, foreground=FG_DEFAULT,
                     fieldbackground=BG_DARK, borderwidth=0)
    style.configure("TFrame", background=BG_MID)
    style.configure("TLabel", background=BG_MID, foreground=FG_DEFAULT,
                     font=(FONT_FAMILY, 11))
    style.configure("Title.TLabel", background=BG_MID, foreground=FG_ACCENT,
                     font=(FONT_FAMILY, 18, "bold"))
    style.configure("Sub.TLabel", background=BG_MID, foreground=FG_DIM,
                     font=(FONT_FAMILY, 10))
    style.configure("TButton", background=BG_LIGHT, foreground=FG_DEFAULT,
                     font=(FONT_FAMILY, 11), padding=(12, 6))
    style.map("TButton", background=[("active", BG_DARK)])
    style.configure("Go.TButton", background="#2d5a88", foreground="#ffffff",
                     font=(FONT_FAMILY, 12, "bold"), padding=(16, 10))
    style.map("Go.TButton", background=[("active", "#3a7bc8")])
    style.configure("Join.TButton", background="#1a7a4c", foreground="#ffffff",
                     font=(FONT_FAMILY, 12, "bold"), padding=(16, 10))
    style.map("Join.TButton", background=[("active", "#22a366")])
    style.configure("TEntry", fieldbackground=BG_DARK,
                     foreground=FG_DEFAULT, insertcolor=FG_DEFAULT)


def _center_and_lift(root: tk.Tk) -> None:
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"+{x}+{y}")
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()


class StartupDialog:
    """First screen: choose Create Channel or Join Channel."""

    def __init__(self) -> None:
        self.result: dict[str, Any] | None = None

        self.root = tk.Tk()
        self.root.title("P2P Chat")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_MID)
        _apply_dialog_styles(self.root)

        frame = ttk.Frame(self.root, padding=40)
        frame.pack()

        ttk.Label(frame, text="P2P Chat", style="Title.TLabel").pack(
            pady=(0, 6))
        ttk.Label(frame, text="Select an option to get started",
                  style="Sub.TLabel").pack(pady=(0, 28))

        # ── Username (shared) ───────────────────────────────────────
        ttk.Label(frame, text="Username").pack(anchor=tk.W)
        self.ent_username = ttk.Entry(frame, width=34,
                                       font=(FONT_FAMILY, 11))
        self.ent_username.insert(0, f"user-{random.randint(1000, 9999)}")
        self.ent_username.pack(fill=tk.X, pady=(2, 18))
        self.ent_username.select_range(0, tk.END)
        self.ent_username.focus_set()

        # ── Create Channel ──────────────────────────────────────────
        sep1 = ttk.Frame(frame)
        sep1.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(sep1, text="Port to host on").pack(anchor=tk.W)
        self.ent_port = ttk.Entry(sep1, width=34, font=(FONT_FAMILY, 11))
        self.ent_port.insert(0, str(random.randint(5000, 5999)))
        self.ent_port.pack(fill=tk.X, pady=(2, 8))
        ttk.Button(sep1, text="Create Channel", style="Go.TButton",
                   command=self._on_create).pack(fill=tk.X)

        # ── Divider ────────────────────────────────────────────────
        div = ttk.Frame(frame, height=1)
        div.pack(fill=tk.X, pady=18)
        tk.Frame(div, bg=FG_DIM, height=1).pack(fill=tk.X)

        # ── Join Channel ────────────────────────────────────────────
        sep2 = ttk.Frame(frame)
        sep2.pack(fill=tk.X, pady=(0, 0))
        ttk.Label(sep2, text="Host address (host:port)").pack(anchor=tk.W)
        self.ent_connect = ttk.Entry(sep2, width=34,
                                      font=(FONT_FAMILY, 11))
        self.ent_connect.insert(0, "127.0.0.1:5000")
        self.ent_connect.pack(fill=tk.X, pady=(2, 8))
        ttk.Button(sep2, text="Join Channel", style="Join.TButton",
                   command=self._on_join).pack(fill=tk.X)

        # ── Error label ─────────────────────────────────────────────
        self.lbl_error = ttk.Label(frame, text="", foreground=FG_RED)
        self.lbl_error.pack(pady=(12, 0))

        _center_and_lift(self.root)

    def _validate_username(self) -> str | None:
        name = self.ent_username.get().strip()
        if not name:
            self.lbl_error.configure(text="Username cannot be empty.")
            return None
        return name

    def _on_create(self) -> None:
        username = self._validate_username()
        if not username:
            return
        port_str = self.ent_port.get().strip()
        try:
            port = int(port_str)
            if not (1024 <= port <= 65535):
                raise ValueError
        except ValueError:
            self.lbl_error.configure(text="Port must be 1024–65535.")
            return
        self.result = {
            "mode": "host",
            "host": "0.0.0.0",
            "port": port,
            "username": username,
        }
        self.root.destroy()

    def _on_join(self) -> None:
        username = self._validate_username()
        if not username:
            return
        addr = self.ent_connect.get().strip()
        if ":" not in addr:
            self.lbl_error.configure(text="Address must be host:port.")
            return
        host_part, p_str = addr.rsplit(":", 1)
        try:
            port = int(p_str)
        except ValueError:
            self.lbl_error.configure(text="Invalid port in address.")
            return
        # Client gets a random local port (not visible to user).
        self.result = {
            "mode": "client",
            "host": "127.0.0.1",
            "port": random.randint(10000, 60000),
            "username": username,
            "connect": f"{host_part}:{port}",
        }
        self.root.destroy()

    def run(self) -> dict[str, Any] | None:
        self.root.mainloop()
        return self.result


# ═══════════════════════════════════════════════════════════════════
#  launch_gui()  — the "master command".  No CLI args required.
#  Shows StartupDialog → creates Node/VoiceEngine → opens ChatGUI.
# ═══════════════════════════════════════════════════════════════════

def launch_gui() -> None:
    """One-click entry point: startup dialog → chat window."""

    dialog = StartupDialog()
    config = dialog.run()
    if config is None:
        return

    mode = config["mode"]
    host = config["host"]
    port = config["port"]
    username = config["username"]
    is_host = mode == "host"
    udp_port = port

    node = Node(host, port, username, is_host=is_host)
    # Bind 0.0.0.0 for listen, but advertise a real IP in node_id (hello / voice).
    if is_host and host == "0.0.0.0":
        node.node_id = f"{detect_lan_ip()}:{port}"

    voice = VoiceEngine(node.node_id, username, host, udp_port,
                        is_host=is_host)

    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run_loop, daemon=True, name="asyncio")
    thread.start()

    startup_error: str | None = None

    async def _startup() -> None:
        nonlocal startup_error
        try:
            await node.start()
        except OSError as e:
            startup_error = f"Cannot listen on port {port}:\n{e}"
            return
        await voice.start()
        if not is_host:
            h, p_str = config["connect"].rsplit(":", 1)
            await node.connect_to(h, int(p_str))
            if not node.peers:
                startup_error = (
                    f"Cannot connect to {config['connect']}\n\n"
                    "Make sure the host has created a channel first."
                )

    future = asyncio.run_coroutine_threadsafe(_startup(), loop)
    try:
        future.result(timeout=_STARTUP_TIMEOUT)
    except Exception as exc:
        startup_error = f"Startup failed:\n{type(exc).__name__}: {exc}"

    if startup_error:
        err = tk.Tk()
        err.withdraw()
        from tkinter import messagebox
        messagebox.showerror("Startup Error", startup_error)
        err.destroy()

        async def _cleanup_failed_startup() -> None:
            try:
                await voice.stop()
            except Exception:
                pass
            try:
                await node.stop()
            except Exception:
                pass

        fut = asyncio.run_coroutine_threadsafe(_cleanup_failed_startup(), loop)
        try:
            fut.result(timeout=_ASYNC_SHUTDOWN_TIMEOUT)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        return

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
