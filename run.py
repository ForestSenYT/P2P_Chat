"""Double-click this file to launch P2P Chat (GUI mode)."""

from __future__ import annotations

import sys
import traceback


def main() -> None:
    try:
        from p2p_chat.gui import launch_gui

        launch_gui()
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception:
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
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
