"""What you see before anything is open.

It used to be one line of grey text in the middle of an empty canvas, framed
by a tool column, a panel and a page grid that were all disabled - a first
screen made mostly of things you could not use yet. This says what the app is
for, offers the two ways in, lists what you were working on last, and says in
one line how the work goes.

Dropping a folder anywhere on the window still works; the pane deliberately
does not accept drops itself, so they fall through to the window's handler,
which is the one route every drop takes.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)

from . import icons, theme

STEPS = (
    ("Crop", "click with the crop tool, pull the edges in"),
    ("Straighten", "guides, auto, or the angle slider"),
    ("Put in order", "drag pages in the grid"),
    ("Export", "TIFFs, a zip and a PDF in one go"),
)


class WelcomePane(QWidget):
    openFolder = Signal()
    openProject = Signal()
    openRecent = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("welcome")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"#welcome {{ background:{theme.CANVAS}; }}")

        outer = QVBoxLayout(self)
        outer.addStretch(3)
        col = QVBoxLayout()
        col.setSpacing(10)

        mark = QLabel()
        pm = QPixmap(72, 72)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(72 / 64.0, 72 / 64.0)
        icons.draw_app_icon(p, detail=True)
        p.end()
        mark.setPixmap(pm)
        mark.setAlignment(Qt.AlignCenter)
        col.addWidget(mark)

        title = QLabel("ScanForge")
        f = QFont(title.font())
        f.setPointSizeF(f.pointSizeF() * 2.0)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)
        col.addWidget(title)
        sub = QLabel("Crop, straighten and put in order high-resolution scans,\n"
                     "without ever touching the originals.")
        sub.setAlignment(Qt.AlignCenter)
        theme.hint(sub)
        col.addWidget(sub)
        col.addSpacing(14)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.btn_folder = QPushButton("Open a folder of scans…")
        theme.primary(self.btn_folder)
        self.btn_folder.setToolTip("Ctrl+O")
        self.btn_folder.clicked.connect(self.openFolder.emit)
        self.btn_project = QPushButton("Open a project…")
        self.btn_project.setToolTip("Ctrl+Alt+O")
        self.btn_project.setMinimumHeight(self.btn_folder.sizeHint().height())
        self.btn_project.clicked.connect(self.openProject.emit)
        buttons.addWidget(self.btn_folder)
        buttons.addWidget(self.btn_project)
        buttons.addStretch(1)
        col.addLayout(buttons)
        drop = QLabel("or drop a folder anywhere in this window")
        drop.setAlignment(Qt.AlignCenter)
        theme.faint(drop)
        col.addWidget(drop)

        # recent projects, filled in by set_recent()
        self.recent_box = QWidget()
        self.recent_layout = QVBoxLayout(self.recent_box)
        self.recent_layout.setContentsMargins(0, 18, 0, 0)
        self.recent_layout.setSpacing(2)
        col.addWidget(self.recent_box)

        col.addSpacing(26)
        steps = QHBoxLayout()
        steps.setSpacing(26)
        steps.addStretch(1)
        for n, (name, how) in enumerate(STEPS, 1):
            box = QVBoxLayout()
            box.setSpacing(2)
            head = QLabel(f"{n}  {name}")
            theme.heading(head)
            head.setAlignment(Qt.AlignCenter)
            what = QLabel(how)
            theme.faint(what)
            what.setAlignment(Qt.AlignCenter)
            box.addWidget(head)
            box.addWidget(what)
            steps.addLayout(box)
        steps.addStretch(1)
        col.addLayout(steps)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addLayout(col)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(4)
        self.set_recent([])

    def set_recent(self, paths: list) -> None:
        while self.recent_layout.count():
            item = self.recent_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        paths = [p for p in paths if os.path.exists(p)][:6]
        self.recent_box.setVisible(bool(paths))
        if not paths:
            return
        head = QLabel("Recent projects")
        theme.hint(head)
        head.setAlignment(Qt.AlignCenter)
        self.recent_layout.addWidget(head)
        for path in paths:
            b = QPushButton(os.path.splitext(os.path.basename(path))[0])
            b.setToolTip(path)
            b.setFlat(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton { border:0; background:transparent; padding:3px 8px;"
                " color:#7fb0e6; }"
                "QPushButton:hover { color:#a9ccf2; text-decoration:underline; }")
            b.clicked.connect(lambda _=False, p=path: self.openRecent.emit(p))
            self.recent_layout.addWidget(b, 0, Qt.AlignHCenter)
