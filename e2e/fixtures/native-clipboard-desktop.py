"""Minimal native desktop text editor for the isolated KasmVNC clipboard E2E."""

from __future__ import annotations

import pathlib
import sys
import tkinter as tk

output_path = pathlib.Path(sys.argv[1])
root = tk.Tk()
root.title("OpenCuria native clipboard keyboard fixture")
root.geometry("760x420+150+140")
root.configure(background="#f4f4f5")
toolbar = tk.Frame(root, background="#f4f4f5")
toolbar.pack(fill="x", padx=10, pady=(10, 4))
label = tk.Label(
    toolbar,
    text="Native desktop clipboard test · click the text field and press Ctrl+V",
    anchor="w",
    background="#f4f4f5",
    foreground="#18181b",
    font=("Sans", 11),
)
label.pack(side="left", fill="x", expand=True)
text = tk.Text(root, wrap="word", undo=True, font=("Sans", 14), padx=8, pady=8)
text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
text.insert("1.0", "Remote clipboard seed — 東京\nSecond line from the VM")


def persist_text(_event: tk.Event | None = None) -> None:
    """Persist text for real native GUI paste verification by the E2E."""
    output_path.write_text(text.get("1.0", "end-1c"), encoding="utf-8")


def select_all(_event: tk.Event) -> str:
    """Provide conventional Ctrl+A selection in this native Linux text field."""
    text.tag_add("sel", "1.0", "end-1c")
    text.mark_set("insert", "1.0")
    text.see("insert")
    return "break"


def copy_selection(_event: tk.Event) -> str:
    """Copy the current native GUI selection through the X11 clipboard."""
    try:
        selected = text.get("sel.first", "sel.last")
    except tk.TclError:
        selected = text.get("1.0", "end-1c")
    output_path.with_suffix(".copy-event").write_text(selected, encoding="utf-8")
    root.clipboard_clear()
    root.clipboard_append(selected, type="UTF8_STRING")
    root.update_idletasks()
    return "break"


def select_on_click(_event: tk.Event) -> None:
    """Select fixture contents after the native text widget receives a click."""
    root.after_idle(lambda: text.tag_add("sel", "1.0", "end-1c"))


text.bind("<Button-1>", select_on_click, add="+")


def clipboard_shortcut(event: tk.Event) -> str | None:
    """Handle ordinary clipboard shortcuts in this minimal desktop fixture."""
    if not event.state & 0x4:
        return None
    if event.keysym.lower() == "a":
        return select_all(event)
    if event.keysym.lower() == "c":
        return copy_selection(event)
    if event.keysym.lower() == "v":
        root.after_idle(persist_text)
    return None


text.bind("<Control-KeyPress>", clipboard_shortcut, add="+")
text.bind("<KeyRelease>", persist_text)
text.bind("<<Paste>>", lambda _event: root.after_idle(persist_text))
text.focus_force()
root.after(500, text.focus_force)
persist_text()
root.mainloop()
