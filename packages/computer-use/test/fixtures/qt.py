"""Qt fixture whose accessibility bridge uses the desktop's AT-SPI status."""

import os
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

application = QApplication([])
window = QWidget()
window.setWindowTitle(os.environ["CUA_TEST_TITLE"])
window.resize(600, 300)
layout = QVBoxLayout(window)
entry = QLineEdit()
entry.setAccessibleName("Verification text")
button = QPushButton("Save verification")
status = QLabel("Not saved")


def save() -> None:
    """Persist the entered value as independent evidence of the AX action."""
    Path(os.environ["CUA_TEST_RESULT"]).write_text(entry.text())
    status.setText("Saved: " + entry.text())


button.clicked.connect(save)
for child in (entry, button, status):
    layout.addWidget(child)
window.show()
application.exec_()
