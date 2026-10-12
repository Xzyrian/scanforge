"""F1: every shortcut, grouped, on one sheet.

This used to be a message box of plain text that had drifted from the app it
described - it still listed D for an auto-deskew that had been removed, and
Delete for two different things without saying which was which. It is a
structured list now, and the UI tests check every key in it against the
actions the window actually has.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout, QLabel,
                               QScrollArea, QVBoxLayout, QWidget)

from . import theme

# (heading, [(keys, what it does), ...]).  Keys are written as the app shows
# them; several alternatives are separated by " / ".
SECTIONS = [
    ("Tools", [
        ("V", "Cursor - pan, zoom, drag crop and blur handles"),
        ("C", "Crop this page - drag, or click for the whole page"),
        ("Shift+C", "Crop every page"),
        ("L", "Edit all pages with this crop (on / off)"),
        ("B", "Blur a spot"),
        ("X", "Cut into pages - Shift cuts across"),
        ("Ctrl+Del", "Delete the selected blur"),
        ("Esc", "Back to the cursor"),
    ]),
    ("Straightening", [
        ("[ / ]", "Rotate 0.01°  ·  with Shift, 0.10°"),
        ("A", "Auto straighten and crop"),
        ("Space", "Level guide across the view"),
        ("Shift+Space", "Upright guide across the view"),
    ]),
    ("Cropping", [
        ("Arrows", "Nudge the crop 1 px  ·  Shift 10 px  ·  Alt resizes"),
        ("E", "Fit the crop to the page edges"),
        ("Ctrl+D", "Clear the crop"),
        ("Ctrl+R", "Reset the page - crop, angle, turns and flips"),
        ("Shift while drawing", "Let the crop go past the scan's edge"),
    ]),
    ("Viewing", [
        ("Ctrl+← / Ctrl+→", "Previous / next page"),
        ("Enter", "Mark done and go to the next page"),
        ("Ctrl+Enter", "Mark done / not done"),
        ("Wheel", "Scroll  ·  Shift+wheel across  ·  Ctrl+wheel zoom"),
        ("Ctrl++ / Ctrl+-", "Zoom in / out"),
        ("F", "Fit the page in the view"),
        ("G", "Show the half-inch grid"),
        ("Middle-drag", "Pan"),
    ]),
    ("Pages", [
        ("Drag", "Put pages in order"),
        ("Ctrl+Shift+↑ / ↓", "Move the selected pages up / down"),
        ("Ctrl+M", "Move the selected pages to a position"),
        ("Ctrl+J", "Align or join two pages"),
        ("Delete", "Remove the selected pages - or, on the page, the "
                   "selected blur. Files on disk are never touched"),
    ]),
    ("Reusing framing", [
        ("Ctrl+B", "Copy framing from the previous page"),
        ("Ctrl+Shift+B", "Apply this framing to the selected pages"),
        ("Ctrl+Shift+M", "Match crops that are a few pixels apart"),
    ]),
    ("Document", [
        ("Ctrl+N", "New project"),
        ("Ctrl+O", "Open a folder of scans"),
        ("Ctrl+Shift+O", "Add more scans"),
        ("Ctrl+Alt+O", "Open a project"),
        ("Ctrl+S / Ctrl+Shift+S", "Save / save as"),
        ("Ctrl+G", "Set the document name"),
        ("Ctrl+P", "Preview as a book"),
        ("Ctrl+E / Ctrl+Shift+E", "Export all / export the selected pages"),
        ("Ctrl+Z / Ctrl+Y", "Undo / redo - batch operations included"),
        ("F1", "This sheet"),
    ]),
]

_KEY_CSS = ("background:#3a3d45; border:1px solid #4a4e57; border-radius:3px;"
            " padding:1px 6px; color:#e6e9ee;")


def _section(title: str, rows: list) -> QWidget:
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(0, 0, 0, 0)
    g.setHorizontalSpacing(12)
    g.setVerticalSpacing(5)
    head = QLabel(title)
    theme.heading(head)
    g.addWidget(head, 0, 0, 1, 2)
    for r, (keys, what) in enumerate(rows, 1):
        key = QLabel(keys)
        key.setStyleSheet(_KEY_CSS)
        key.setAlignment(Qt.AlignCenter)
        cell = QWidget()
        cell.setFixedWidth(150)          # every section's text starts level
        cl = QGridLayout(cell)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(key, 0, 0, Qt.AlignLeft | Qt.AlignTop)
        text = QLabel(what)
        text.setWordWrap(True)
        theme.hint(text)
        g.addWidget(cell, r, 0, Qt.AlignTop)
        g.addWidget(text, r, 1)
    g.setColumnStretch(1, 1)
    return w


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(900, 700)
        root = QVBoxLayout(self)

        body = QWidget()
        grid = QGridLayout(body)
        grid.setHorizontalSpacing(34)
        grid.setVerticalSpacing(22)
        half = (len(SECTIONS) + 1) // 2
        for i, (title, rows) in enumerate(SECTIONS):
            col, row = (0, i) if i < half else (1, i - half)
            grid.addWidget(_section(title, rows), row, col, Qt.AlignTop)
        grid.setRowStretch(half, 1)
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        root.addWidget(scroll, 1)

        tip = QLabel("Hover over any button for its name and key; the bar at "
                     "the bottom of the window says what it does.")
        theme.faint(tip)
        root.addWidget(tip)
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        box.button(QDialogButtonBox.Close).clicked.connect(self.accept)
        root.addWidget(box)
