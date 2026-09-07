"""The straightening strip, under the canvas.

Straightening is the one adjustment you make while staring at the page, judging
a baseline against a guide by eye, so the control belongs directly beneath the
page rather than in a panel off to one side.  It is three things and no more: a
slider for the coarse move, a box for the exact number, and a button back to
zero.

What used to sit here as well, and does not any more:

* six nudge buttons (-0.10 -0.05 -0.01 +0.01 +0.05 +0.10).  The `[` and `]`
  keys already step by 0.01, and by 0.10 with Shift, without moving the mouse
  off the page you are judging.
* an "Auto-deskew" button, which measured the skew and applied it.  Auto
  straighten + crop does the same measurement and then finds the sheet as well,
  so the deskew-only button was a worse version of a button beside it.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton,
                               QSlider, QWidget)

from ..model import PageEdit

# The slider spans a range a scanner actually produces.  A sheet that lands more
# than a couple of degrees out was put on the glass crooked, and that is a job
# for the rotate buttons or the number box, not for a slider whose every pixel
# would then be worth a tenth of a degree.
SPAN = 2.0


class AngleStrip(QWidget):
    angleChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit: Optional[PageEdit] = None
        self._loading = False

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 5, 10, 5)
        h.setSpacing(8)

        lbl = QLabel("Angle")
        lbl.setStyleSheet("color:#9aa0aa;")
        h.addWidget(lbl)

        self.spin = QDoubleSpinBox()
        self.spin.setRange(-45.0, 45.0)
        self.spin.setDecimals(3)
        self.spin.setSingleStep(0.01)
        self.spin.setSuffix(" °")
        self.spin.setFixedWidth(94)
        self.spin.setKeyboardTracking(False)
        self.spin.setToolTip("Type it, or scroll on the box for 0.01° a notch")
        self.spin.valueChanged.connect(self._from_spin)
        h.addWidget(self.spin)

        self.btn_zero = QPushButton("0")
        self.btn_zero.setFixedWidth(30)
        self.btn_zero.setToolTip("Back to square")
        self.btn_zero.clicked.connect(lambda: self.spin.setValue(0.0))
        h.addWidget(self.btn_zero)

        left = QLabel(f"−{SPAN:g}°")
        right = QLabel(f"+{SPAN:g}°")
        for t in (left, right):
            t.setStyleSheet("color:#767c86;font-size:10px;")
        h.addSpacing(6)
        h.addWidget(left)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(-SPAN * 1000), int(SPAN * 1000))
        self.slider.setSingleStep(10)
        self.slider.setPageStep(100)
        self.slider.setTracking(True)
        self.slider.setToolTip(
            f"±{SPAN:g}° across the full width — about 0.01° per screen pixel. "
            "[ and ] step by 0.01°, or 0.10° with Shift.")
        self.slider.valueChanged.connect(self._from_slider)
        # a small minimum: the strip must not set a floor for the whole middle
        # pane and squeeze the page grid beside it
        self.slider.setMinimumWidth(90)
        h.addWidget(self.slider, 1)
        h.addWidget(right)

        self.set_page(None)

    # ---------------------------------------------------------------- state
    def set_page(self, edit: Optional[PageEdit]) -> None:
        self.edit = edit
        on = edit is not None
        for w in (self.spin, self.slider, self.btn_zero):
            w.setEnabled(on)
        self._loading = True
        try:
            self.spin.setValue(edit.angle if on else 0.0)
            self.slider.setValue(self._clamped(edit.angle if on else 0.0))
        finally:
            self._loading = False

    def refresh(self) -> None:
        """Follow an angle changed from somewhere else - a nudge key, the
        automatic pass, an undo."""
        if self.edit is None:
            return
        self._loading = True
        try:
            self.spin.setValue(self.edit.angle)
            self.slider.setValue(self._clamped(self.edit.angle))
        finally:
            self._loading = False

    @staticmethod
    def _clamped(a: float) -> int:
        return int(round(max(-SPAN, min(SPAN, a)) * 1000))

    # ----------------------------------------------------------------- wiring
    def _from_spin(self, val: float) -> None:
        if self._loading:
            return
        self.slider.blockSignals(True)
        self.slider.setValue(self._clamped(val))
        self.slider.blockSignals(False)
        self.angleChanged.emit(round(val, 3))

    def _from_slider(self, val: int) -> None:
        if self._loading:
            return
        a = val / 1000.0
        self.spin.blockSignals(True)
        self.spin.setValue(a)
        self.spin.blockSignals(False)
        self.angleChanged.emit(a)
