"""The options bar: the numbers for the page in front of you, above it.

It replaces the right-hand panel. That panel had grown into a holding
position - half of it repeated buttons already in the tool column (draw a new
crop, fit to page edges, blur a spot, copy framing), its result readout was cut
off at the edge, and with no project open it was a wall of greyed-out
controls. What was *not* a repeat is here, one strip above the canvas:

    Crop   X 140  Y 160  W 2120  H 3050   → 2120 × 3050 px · 3.53 × 5.08 in
           Aspect [Free ▾]   This page [Takes a crop-all ▾]   ✕ Clear
    Angle  0.000° [0]  −2° ─────────●───────── +2°     Blur 1 · pixelate, 100

The crop row wraps onto another line in a narrow window rather than pushing
the page grid aside. The blur readout appears only while a blur is selected.
The page's file, size and resolution moved to the status bar; "marked done"
is the tick at the bottom of the tool column, Enter, and Ctrl+Enter.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QSpinBox, QVBoxLayout, QWidget)

from ..model import PageEdit
from . import theme
from .angle_strip import AngleStrip
from .widgets import FlowLayout, _cluster, _sep

# What "crop all pages" does to this page.  The labels and the three choices
# are unchanged from the panel this came out of: how cropping reaches across
# pages is being worked out separately, and this pass only moves it.
CROP_MODES = {
    "Takes a crop-all": "linked",
    "Keeps its own position": "own_position",
    "Locked — never overwritten": "locked",
}
_MODE_LABEL = {v: k for k, v in CROP_MODES.items()}

ASPECTS = {
    "Free": None,
    "Match source page": -1.0,
    "Square 1:1": 1.0,
    "A4 portrait": 210.0 / 297.0,
    "A5 portrait": 148.0 / 210.0,
    "US Letter portrait": 8.5 / 11.0,
    "US Legal portrait": 8.5 / 14.0,
    "4:3": 4.0 / 3.0,
    "3:2": 3.0 / 2.0,
}


def _label(text: str, tip: str = "") -> QLabel:
    lab = QLabel(text)
    theme.hint(lab)
    if tip:
        lab.setToolTip(tip)
    return lab


class OptionsBar(QWidget):
    cropChanged = Signal(float, float, float, float)
    requestResetCrop = Signal()
    requestBlurDelete = Signal()
    requestBlurConfigure = Signal()
    cropModeChanged = Signal(str)
    aspectChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit: Optional[PageEdit] = None
        self._loading = False
        self.setObjectName("options_bar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"#options_bar {{ background:{theme.PANEL};"
            f" border-bottom:1px solid {theme.BORDER_DARK}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        root.setSpacing(4)

        # --------------------------------------------------------- crop row
        crop_row = QWidget()
        flow = FlowLayout(crop_row, margin=0, hspace=12, vspace=4)

        self.spins = {}
        boxes = [_label("Crop", "The crop, in full-resolution source pixels")]
        for key in ("x", "y", "w", "h"):
            sp = QSpinBox()
            sp.setRange(-200000, 400000)
            sp.setKeyboardTracking(False)
            sp.setFixedWidth(78)
            sp.setToolTip({"x": "Left edge", "y": "Top edge", "w": "Width",
                           "h": "Height"}[key]
                          + ", in full-resolution pixels. Scroll for 1 px.")
            sp.valueChanged.connect(self._crop_spin)
            self.spins[key] = sp
            lab = QLabel(key.upper())
            theme.faint(lab)
            boxes += [lab, sp]
        flow.addWidget(_cluster(*boxes, spacing=4))

        self.lbl_out = _label("")
        self.lbl_out.setToolTip("What this page exports as")
        flow.addWidget(self.lbl_out)

        self.cmb_aspect = QComboBox()
        self.cmb_aspect.addItems(list(ASPECTS.keys()))
        self.cmb_aspect.setToolTip("Hold the crop to a shape while you drag it")
        self.cmb_aspect.currentTextChanged.connect(self._aspect)
        flow.addWidget(_cluster(_label("Aspect"), self.cmb_aspect))

        self.cmb_cropmode = QComboBox()
        self.cmb_cropmode.addItems(list(CROP_MODES.keys()))
        self.cmb_cropmode.setToolTip(
            "What \"crop all pages\" does to this page.\n"
            "Spread-aligned, cut and joined pages are locked automatically so "
            "their framing cannot be overwritten.")
        self.cmb_cropmode.currentTextChanged.connect(self._cropmode)
        flow.addWidget(_cluster(_label("This page"), self.cmb_cropmode))

        self.btn_clear = QPushButton("Clear crop")
        self.btn_clear.setToolTip("Remove this page's crop  (Ctrl+D)")
        self.btn_clear.clicked.connect(self.requestResetCrop.emit)
        flow.addWidget(self.btn_clear)
        root.addWidget(crop_row)

        # -------------------------------------------------------- angle row
        angle_row = QHBoxLayout()
        angle_row.setContentsMargins(0, 0, 0, 0)
        angle_row.setSpacing(10)
        self.angle = AngleStrip()
        self.angle.layout().setContentsMargins(0, 0, 0, 0)
        angle_row.addWidget(self.angle, 1)

        # the selected blur, only while there is one
        self.blur_box = QWidget()
        bl = QHBoxLayout(self.blur_box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        bl.addWidget(_sep())
        self.lbl_blur = _label("")
        bl.addWidget(self.lbl_blur)
        self.btn_blur_edit = QPushButton("Blur settings…")
        self.btn_blur_edit.setToolTip(
            "Change this blur's style and strength - or double-click it")
        self.btn_blur_edit.clicked.connect(self.requestBlurConfigure.emit)
        self.btn_blur_del = QPushButton("Delete")
        self.btn_blur_del.setToolTip("Remove this blur  (Ctrl+Del)")
        self.btn_blur_del.clicked.connect(self.requestBlurDelete.emit)
        bl.addWidget(self.btn_blur_edit)
        bl.addWidget(self.btn_blur_del)
        self.blur_box.setVisible(False)
        angle_row.addWidget(self.blur_box)
        root.addLayout(angle_row)

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.set_page(None)

    # ------------------------------------------------------------------ load
    def set_page(self, edit: Optional[PageEdit]) -> None:
        self.edit = edit
        on = edit is not None
        for w in (*self.spins.values(), self.cmb_aspect, self.cmb_cropmode,
                  self.btn_clear):
            w.setEnabled(on)
        self.angle.set_page(edit)
        if not on:
            self.lbl_out.setText("")
            self.set_blur(-1, None)
            return
        self._loading = True
        try:
            self._load_crop()
            self.cmb_cropmode.setCurrentText(
                _MODE_LABEL.get(edit.crop_mode, "Takes a crop-all"))
        finally:
            self._loading = False
        self.refresh_output()

    def _load_crop(self) -> None:
        e = self.edit
        if e is None:
            return
        from ..geometry import rotated_size
        if e.crop:
            x, y, w, h = e.crop
        else:
            bw, bh = e.base_size()
            W, H = rotated_size(bw, bh, e.angle)
            x, y, w, h = 0, 0, W, H
        for key, val in zip(("x", "y", "w", "h"), (x, y, w, h)):
            self.spins[key].blockSignals(True)
            self.spins[key].setValue(int(round(val)))
            self.spins[key].blockSignals(False)
        self.btn_clear.setEnabled(e.crop is not None)

    def refresh(self) -> None:
        """Re-read values from the page after the canvas changed something."""
        if self.edit is None:
            return
        self._loading = True
        try:
            self._load_crop()
        finally:
            self._loading = False
        self.angle.refresh()
        self.refresh_output()

    def refresh_output(self) -> None:
        e = self.edit
        if e is None:
            self.lbl_out.setText("")
            return
        w, h = e.output_size()
        iw, ih = e.output_inches()
        size = f"{w} × {h} px · {iw:.2f} × {ih:.2f} in"
        if e.crop is None:
            # the numbers still show the whole page, which is what exports -
            # but say so, so a whole-page readout is not mistaken for a crop
            self.lbl_out.setText(f"No crop — exports the whole scan, {size}")
        else:
            self.lbl_out.setText(f"→ {size}")
        self.lbl_out.setToolTip(
            f"Exports as {w} × {h} pixels, "
            f"{iw:.3f} × {ih:.3f} in ({iw * 25.4:.1f} × {ih * 25.4:.1f} mm)")

    def set_blur(self, index: int, blur: Optional[list]) -> None:
        """Show the blur the canvas has selected, or nothing."""
        on = blur is not None
        self.blur_box.setVisible(on)
        self.btn_blur_edit.setEnabled(on)
        self.btn_blur_del.setEnabled(on)
        if not on:
            self.lbl_blur.setText("")
            return
        style = blur[5] if len(blur) > 5 else "Blur"
        strength = int(round(blur[4] if len(blur) > 4 else 8))
        self.lbl_blur.setText(f"Blur {index + 1} · {style.lower()}, {strength}")

    # -------------------------------------------------------------- handlers
    def _crop_spin(self) -> None:
        if self._loading or self.edit is None:
            return
        self.cropChanged.emit(float(self.spins["x"].value()),
                              float(self.spins["y"].value()),
                              float(max(1, self.spins["w"].value())),
                              float(max(1, self.spins["h"].value())))

    def _aspect(self, name: str) -> None:
        val = ASPECTS.get(name)
        if val == -1.0 and self.edit is not None:
            bw, bh = self.edit.base_size()
            val = bw / float(bh) if bh else None
        self.aspectChanged.emit(val)

    def _cropmode(self, label: str) -> None:
        if self._loading:
            return
        self.cropModeChanged.emit(CROP_MODES.get(label, "linked"))
