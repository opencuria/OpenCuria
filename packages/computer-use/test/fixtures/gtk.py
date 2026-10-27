"""Isolated GTK application for real accessibility and input contracts."""

import os
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402 - select the GI namespace first

window = Gtk.Window(
    title=os.environ.get("CUA_TEST_TITLE", "OpenCuria Computer Use Test")
)
window.set_default_size(600, 400)
window.move(80, 100)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
box.set_border_width(20)
entry = Gtk.Entry()
entry.set_placeholder_text("Enter verification text")
entry.get_accessible().set_name("Verification text")
button = Gtk.Button(label="Save verification")
status = Gtk.Label(label="Not saved")


def save(_: Gtk.Button) -> None:
    Path(os.environ["CUA_TEST_RESULT"]).write_text(entry.get_text())
    status.set_text("Saved: " + entry.get_text())


button.connect("clicked", save)
for child in (entry, button, status):
    box.pack_start(child, False, False, 0)
window.add(box)
window.connect("destroy", Gtk.main_quit)
window.show_all()
Gtk.main()
