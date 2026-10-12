"""How a blur looks: asked once, then remembered.

A blur is placed to hide something - a previous owner's name, a price sticker,
a serial number - and the only question that matters is whether it is actually
illegible. That is a judgement about the pixels, so the controls belong next to
the blur rather than in a panel on the far side of the window: the dialog opens
where you put the blur, and the page redraws behind it as you move the slider.

It appears the first time you place a blur in a session, so the settings are
chosen once rather than asked for on every one, and again whenever you
double-click a blur to change it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QRadioButton, QSlider, QSpinBox, QVBoxLayout)
from . import theme

DEFAULT_STYLE = "Pixelate"
DEFAULT_STRENGTH = 100.0
STRENGTH_MIN, STRENGTH_MAX = 2, 300


class BlurDialog(QDialog):
    """Style and strength for one blur, previewed live on the page behind it."""

    changed = Signal(float, str)          # strength, style - as you adjust

    def __init__(self, style: str = DEFAULT_STYLE,
                 strength: float = DEFAULT_STRENGTH, parent=None,
                 first_time: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Blur")
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setSpacing(9)

        if first_time:
            hint = QLabel("You can move and resize the blur afterwards, and\n"
                          "double-click it to come back here.")
            theme.hint(hint)
            root.addWidget(hint)

        self.rb_pixelate = QRadioButton("Pixelate")
        self.rb_pixelate.setToolTip(
            "Replace the area with coarse blocks. Nothing can be recovered "
            "from it, which is why it is the default.")
        self.rb_blur = QRadioButton("Blur")
        self.rb_blur.setToolTip("Soften the area. Gentler, and less final.")
        (self.rb_pixelate if style != "Blur" else self.rb_blur).setChecked(True)
        row = QHBoxLayout()
        row.addWidget(QLabel("Style"))
        row.addWidget(self.rb_pixelate)
        row.addWidget(self.rb_blur)
        row.addStretch(1)
        root.addLayout(row)

        self.sld = QSlider(Qt.Horizontal)
        self.sld.setRange(STRENGTH_MIN, STRENGTH_MAX)
        self.sp = QSpinBox()
        self.sp.setRange(STRENGTH_MIN, STRENGTH_MAX)
        self.sp.setKeyboardTracking(False)
        self.sp.setSuffix(" px")
        val = int(round(max(STRENGTH_MIN, min(STRENGTH_MAX, strength))))
        self.sld.setValue(val)
        self.sp.setValue(val)
        rows = QHBoxLayout()
        rows.addWidget(QLabel("Strength"))
        rows.addWidget(self.sld, 1)
        rows.addWidget(self.sp)
        root.addLayout(rows)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        self._guard = False
        self.sld.valueChanged.connect(self._from_slider)
        self.sp.valueChanged.connect(self._from_spin)
        for rb in (self.rb_pixelate, self.rb_blur):
            rb.toggled.connect(lambda _on: self._emit())

        self.setMinimumWidth(340)

    # ------------------------------------------------------------------ state
    def style(self) -> str:
        return "Pixelate" if self.rb_pixelate.isChecked() else "Blur"

    def strength(self) -> float:
        return float(self.sp.value())

    def values(self) -> tuple:
        return self.strength(), self.style()

    # ------------------------------------------------------------------ slots
    def _from_slider(self, v: int) -> None:
        if self._guard:
            return
        self._guard = True
        self.sp.setValue(v)
        self._guard = False
        self._emit()

    def _from_spin(self, v: int) -> None:
        if self._guard:
            return
        self._guard = True
        self.sld.setValue(v)
        self._guard = False
        self._emit()

    def _emit(self) -> None:
        self.changed.emit(self.strength(), self.style())
