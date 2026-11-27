"""GTK form covering the controls the Computer Use runtime must operate."""

import json
import os
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402 - select the GI namespace first

result = Path(os.environ["CUA_TEST_RESULT"])
window = Gtk.Window(title=os.environ.get("CUA_TEST_TITLE", "OpenCuria GTK form"))
window.set_default_size(700, 560)
window.move(60, 60)
grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin=16)
window.add(grid)

name = Gtk.Entry()
name.set_text("1.0")
name_label = Gtk.Label(label="App name")
name_label.set_mnemonic_widget(name)
grid.attach(name_label, 0, 0, 1, 1)
grid.attach(name, 1, 0, 2, 1)

language = Gtk.ComboBoxText()
for item in ("English", "Deutsch", "Français"):
    language.append_text(item)
language.set_active(0)
language_label = Gtk.Label(label="Language")
language_label.set_mnemonic_widget(language)
grid.attach(language_label, 0, 1, 1, 1)
grid.attach(language, 1, 1, 2, 1)

agree = Gtk.CheckButton(label="I agree")
grid.attach(agree, 0, 2, 2, 1)

amount = Gtk.SpinButton.new_with_range(0, 100, 1)
amount_label = Gtk.Label(label="Amount")
amount_label.set_mnemonic_widget(amount)
grid.attach(amount_label, 0, 3, 1, 1)
grid.attach(amount, 1, 3, 1, 1)

# Unlabeled radio buttons whose meaning comes from the surrounding grid.
grid.attach(Gtk.Label(label="Question"), 0, 4, 1, 1)
grid.attach(Gtk.Label(label="No"), 1, 4, 1, 1)
grid.attach(Gtk.Label(label="Yes"), 2, 4, 1, 1)
grid.attach(Gtk.Label(label="Contains ads"), 0, 5, 1, 1)
radio_no = Gtk.RadioButton()
radio_yes = Gtk.RadioButton.new_from_widget(radio_no)
grid.attach(radio_no, 1, 5, 1, 1)
grid.attach(radio_yes, 2, 5, 1, 1)

notes = Gtk.TextView()
notes.get_accessible().set_name("Notes")
notes.set_size_request(400, 80)
grid.attach(notes, 0, 6, 3, 1)

chooser = Gtk.FileChooserButton(
    title="Choose a document", action=Gtk.FileChooserAction.OPEN
)
chooser_label = Gtk.Label(label="Document")
chooser_label.set_mnemonic_widget(chooser)
grid.attach(chooser_label, 0, 7, 1, 1)
grid.attach(chooser, 1, 7, 2, 1)

status = Gtk.Label(label="Idle")
grid.attach(status, 0, 9, 3, 1)


def load(_button: Gtk.Button) -> None:
    status.set_text("Processing…")

    def finish() -> bool:
        status.set_text("Loaded 3 items")
        for index in range(3):
            grid.attach(Gtk.Label(label=f"Loaded item {index}"), 0, 10 + index, 2, 1)
        window.show_all()
        return False

    GLib.timeout_add(1200, finish)


def save(_button: Gtk.Button) -> None:
    buffer = notes.get_buffer()
    data = {
        "name": name.get_text(),
        "language": language.get_active_text(),
        "agree": agree.get_active(),
        "amount": amount.get_value_as_int(),
        "ads": "yes" if radio_yes.get_active() else "no",
        "notes": buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True),
        "document": chooser.get_filename(),
    }
    result.write_text(json.dumps(data, ensure_ascii=False))
    status.set_text("Saved")


buttons = Gtk.Box(spacing=8)
for label, handler in (("Load data", load), ("Save form", save)):
    button = Gtk.Button(label=label)
    button.connect("clicked", handler)
    buttons.pack_start(button, False, False, 0)
grid.attach(buttons, 0, 8, 3, 1)
window.connect("destroy", Gtk.main_quit)
window.show_all()
Gtk.main()
