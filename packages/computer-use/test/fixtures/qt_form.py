"""Qt form covering the controls the Computer Use runtime must operate."""

import json
import os
from pathlib import Path

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QWidget,
)

application = QApplication([])
window = QWidget()
window.setWindowTitle(os.environ.get("CUA_TEST_TITLE", "OpenCuria Qt form"))
window.resize(640, 520)
window.move(120, 80)
grid = QGridLayout(window)
result = Path(os.environ["CUA_TEST_RESULT"])
chosen: list[str] = []

name = QLineEdit("1.0")
name_label = QLabel("App name")
name_label.setBuddy(name)
grid.addWidget(name_label, 0, 0)
grid.addWidget(name, 0, 1, 1, 2)
language = QComboBox()
language.addItems(["English", "Deutsch", "Français"])
language.setAccessibleName("Language")
grid.addWidget(QLabel("Language"), 1, 0)
grid.addWidget(language, 1, 1, 1, 2)
agree = QCheckBox("I agree")
grid.addWidget(agree, 2, 0, 1, 2)
amount = QSpinBox()
amount.setRange(0, 100)
amount.setAccessibleName("Amount")
grid.addWidget(QLabel("Amount"), 3, 0)
grid.addWidget(amount, 3, 1)
grid.addWidget(QLabel("Question"), 4, 0)
grid.addWidget(QLabel("No"), 4, 1)
grid.addWidget(QLabel("Yes"), 4, 2)
grid.addWidget(QLabel("Contains ads"), 5, 0)
radio_no = QRadioButton()
radio_yes = QRadioButton()
grid.addWidget(radio_no, 5, 1)
grid.addWidget(radio_yes, 5, 2)
notes = QPlainTextEdit()
notes.setAccessibleName("Notes")
grid.addWidget(notes, 6, 0, 1, 3)
status = QLabel("Idle")
grid.addWidget(status, 9, 0, 1, 3)


def choose() -> None:
    files, _ = QFileDialog.getOpenFileNames(
        window, "Choose screenshots", "/tmp", options=QFileDialog.DontUseNativeDialog
    )
    chosen[:] = files
    status.setText(f"Chose {len(files)}")


def load() -> None:
    status.setText("Processing…")

    def finish() -> None:
        status.setText("Loaded 3 items")
        for index in range(3):
            grid.addWidget(QLabel(f"Loaded item {index}"), 10 + index, 0)

    QTimer.singleShot(1200, finish)


def save() -> None:
    result.write_text(
        json.dumps(
            {
                "name": name.text(),
                "language": language.currentText(),
                "agree": agree.isChecked(),
                "amount": amount.value(),
                "ads": "yes" if radio_yes.isChecked() else "no",
                "notes": notes.toPlainText(),
                "files": [os.path.basename(path) for path in chosen],
            },
            ensure_ascii=False,
        )
    )
    status.setText("Saved")


for column, (label, handler) in enumerate(
    (("Choose files", choose), ("Load data", load), ("Save form", save))
):
    button = QPushButton(label)
    button.clicked.connect(handler)
    grid.addWidget(button, 8, column)
window.show()
application.exec_()
