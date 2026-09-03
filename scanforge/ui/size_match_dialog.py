"""Bring crops that are nearly the same size onto exactly the same size.

A document scanned page by page never comes out to one number of pixels.  The
sheet sits a hair differently on the glass, the crop is drawn by hand, and you
end up with eleven pages at 4960x7016 and three at 4958x7014 - a difference you
cannot see and a printer or a PDF reader certainly can.

So: group the pages by "within a few pixels of each other", show what the groups
actually are, and let one exact size be chosen and pushed onto the group.  The
crop grows or shrinks about its own centre, so the framing does not slide.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QSpinBox,
                               QVBoxLayout, QWidget)

from ..model import Project


class SizeMatchDialog(QDialog):
    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Match crop sizes")
        self.resize(560, 470)
        self.project = project
        self._groups: list = []

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        head = QLabel("Pages whose crops are within a few pixels of each other, "
                      "grouped. Pick a group, set the size you want it to be, "
                      "and every page in it is nudged onto that exact size.")
        head.setWordWrap(True)
        head.setStyleSheet("color:#aeb4bd;")
        v.addWidget(head)

        row = QHBoxLayout()
        row.addWidget(QLabel("Group sizes within"))
        self.sp_tol = QSpinBox()
        self.sp_tol.setRange(0, 200)
        self.sp_tol.setValue(5)
        self.sp_tol.setSuffix(" px")
        self.sp_tol.valueChanged.connect(self._rebuild)
        row.addWidget(self.sp_tol)
        self.chk_locked = QCheckBox("Include locked pages")
        self.chk_locked.setToolTip(
            "Spread-aligned and cut pages are locked because they are meant to "
            "be a different shape from the rest of the document.")
        self.chk_locked.toggled.connect(self._rebuild)
        row.addWidget(self.chk_locked)
        row.addStretch(1)
        v.addLayout(row)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._pick_group)
        v.addWidget(self.list, 1)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Make them"))
        self.sp_w = QSpinBox()
        self.sp_h = QSpinBox()
        for sp in (self.sp_w, self.sp_h):
            sp.setRange(1, 200000)
            sp.setSingleStep(1)          # the +/- the user asked for
            sp.setAccelerated(True)
            sp.setSuffix(" px")
            sp.valueChanged.connect(self._update_note)
        size_row.addWidget(self.sp_w)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.sp_h)
        size_row.addStretch(1)
        v.addLayout(size_row)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#9aa0aa;")
        v.addWidget(self.note)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Match")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)
        self._ok = box.button(QDialogButtonBox.Ok)

        self._rebuild()

    # ---------------------------------------------------------------- content
    def _rebuild(self) -> None:
        self._groups = self.project.size_groups(
            tolerance=self.sp_tol.value(),
            include_locked=self.chk_locked.isChecked())
        self.list.clear()
        for g in self._groups:
            w, h = g["size"]
            dw, dh = g["spread"]
            n = len(g["pages"])
            spread = "all identical" if (dw, dh) == (0, 0) \
                else f"varying by {dw}×{dh} px"
            item = QListWidgetItem(f"{w} × {h} px    ·    {n} "
                                   f"page{'s' if n != 1 else ''}    ·    {spread}")
            item.setData(Qt.UserRole, g)
            self.list.addItem(item)
        if self._groups:
            self.list.setCurrentRow(0)
        else:
            self.note.setText("No pages have a crop yet.")
        self._update_note()

    def _pick_group(self, row: int) -> None:
        if not (0 <= row < len(self._groups)):
            return
        w, h = self._groups[row]["size"]
        for sp, v in ((self.sp_w, w), (self.sp_h, h)):
            sp.blockSignals(True)
            sp.setValue(int(v))
            sp.blockSignals(False)
        self._update_note()

    def _update_note(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self._groups)):
            self.note.setText("")
            if hasattr(self, "_ok"):
                self._ok.setEnabled(False)
            return
        g = self._groups[row]
        w, h = self.sp_w.value(), self.sp_h.value()
        biggest = max((abs(w - s[0]), abs(h - s[1])) for s in g["sizes"])
        self.note.setText(
            f"{len(g['pages'])} pages become exactly {w} × {h} px. "
            f"The largest change is {max(biggest)} px, applied around each "
            f"crop's centre so the framing stays put.")
        self._ok.setEnabled(True)

    # ----------------------------------------------------------------- result
    def result_target(self) -> tuple:
        row = self.list.currentRow()
        if not (0 <= row < len(self._groups)):
            return ([], (0, 0))
        return (list(self._groups[row]["pages"]),
                (self.sp_w.value(), self.sp_h.value()))
