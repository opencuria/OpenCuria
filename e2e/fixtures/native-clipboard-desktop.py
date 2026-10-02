"""Minimal native desktop text editor for the isolated KasmVNC clipboard E2E."""

from __future__ import annotations

import json
import pathlib
import sys
import tkinter as tk

output_path = pathlib.Path(sys.argv[1])
root = tk.Tk()
root.title("OpenCuria native clipboard keyboard fixture")
root.geometry("1024x768+0+0")
clicks_path = output_path.with_suffix(".clicks.json")
click_counts = {
    **{
        name: {"count": 0, "down": 0, "up": 0, "rect": [0, 0, 0, 0]}
        for name in ("sidebar", "modal", "bottom")
    },
    "textRect": [0, 0, 0, 0],
    "textFocus": False,
}
buttons: dict[str, tk.Button] = {}

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


def persist_clicks() -> None:
    """Persist real X11 button press/release observations for Playwright."""
    root.update_idletasks()
    for name, button in buttons.items():
        click_counts[name]["rect"] = [
            button.winfo_rootx(),
            button.winfo_rooty(),
            button.winfo_width(),
            button.winfo_height(),
        ]
    click_counts["textRect"] = [
        text.winfo_rootx(), text.winfo_rooty(), text.winfo_width(), text.winfo_height(),
    ] if "text" in globals() else [0, 0, 0, 0]
    click_counts["textFocus"] = root.focus_get() == text if "text" in globals() else False
    temporary_path = clicks_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(click_counts), encoding="utf-8")
    temporary_path.replace(clicks_path)


def record_click(name: str) -> None:
    """Record Tk's activated native button click callback."""
    click_counts[name]["count"] += 1
    persist_clicks()


def record_button_event(name: str, phase: str) -> None:
    """Record native GUI pointer down/up even if a click is canceled."""
    click_counts[name][phase] += 1
    persist_clicks()


button_row = tk.Frame(root, background="#f4f4f5")
button_row.pack(fill="x", padx=10, pady=4)
for name in ("sidebar", "modal"):
    button = tk.Button(button_row, text=f"Click {name}", command=lambda key=name: record_click(key))
    button.bind("<ButtonPress-1>", lambda _event, key=name: record_button_event(key, "down"))
    button.bind("<ButtonRelease-1>", lambda _event, key=name: record_button_event(key, "up"))
    button.pack(side="left", padx=8)
    buttons[name] = button
bottom_button = tk.Button(root, text="Click bottom overlay area", command=lambda: record_click("bottom"))
bottom_button.place(x=260, y=700, width=197, height=36)
bottom_button.bind("<ButtonPress-1>", lambda _event: record_button_event("bottom", "down"))
bottom_button.bind("<ButtonRelease-1>", lambda _event: record_button_event("bottom", "up"))
buttons["bottom"] = bottom_button

text = tk.Text(root, wrap="word", undo=True, font=("Sans", 14), padx=8, pady=8)
text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
for button in buttons.values():
    button.lift()
root.update_idletasks()
persist_clicks()
root.bind('<Configure>', lambda _event: persist_clicks())
text.insert("1.0", "Remote clipboard seed — 東京\nSecond line from the VM")


def persist_text(_event: tk.Event | None = None) -> None:
    """Persist text for real native GUI paste verification by the E2E."""
    output_path.write_text(text.get("1.0", "end-1c"), encoding="utf-8")


def persist_modified_text(_event: tk.Event) -> None:
    """Observe Tk edits even when a native paste has no matching key release."""
    if text.edit_modified():
        text.edit_modified(False)
        persist_text()


text.bind("<<Modified>>", persist_modified_text, add="+")


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
text.bind("<FocusIn>", lambda _event: persist_clicks(), add="+")
text.bind("<FocusOut>", lambda _event: persist_clicks(), add="+")


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
