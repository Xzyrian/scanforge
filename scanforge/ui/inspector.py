"""The right-hand controls: crop numbers, reuse, blur, output size.

Orientation went to the tool column and straightening to the strip under
the canvas.  What is left here is the numbers you type and the states you
set, which is the shape this panel is being pulled towards.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSizePolicy, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from ..model import PageEdit

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


def _sep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet("color:#3a3d44;")
    return f


class Inspector(QWidget):
    cropChanged = Signal(float, float, float, float)
    requestFitToPage = Signal()
    requestCropTool = Signal()
    requestBlurTool = Signal()
    requestBlurDelete = Signal()
    blurChanged = Signal(float, str)      # radius, mode
    requestResetCrop = Signal()
    cropModeChanged = Signal(str)
    requestSelectAll = Signal()
    requestCopyPrevious = Signal()
    requestApplyToDocument = Signal()
    requestApplyToSelection = Signal()
    aspectChanged = Signal(object)
    doneToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit: Optional[PageEdit] = None
        self._loading = False
        # It shares the right-hand column with the page grid now, so it must
        # not cap that column's width - the grid wants to be as wide as you
        # care to drag it.
        self.setMinimumWidth(280)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ------------------------------------------------------------ header
        self.lbl_name = QLabel("No page selected")
        f = QFont(); f.setBold(True); f.setPointSize(10)
        self.lbl_name.setFont(f)
        self.lbl_name.setWordWrap(True)
        self.lbl_src = QLabel("-")
        self.lbl_src.setStyleSheet("color:#9aa0aa;")
        root.addWidget(self.lbl_name)
        root.addWidget(self.lbl_src)
        root.addWidget(_sep())

        # Orientation moved to the tool column and straightening to the strip
        # under the canvas.  Both act on the page you are looking at, and this
        # panel put them as far from it as the window allows.

        # -------------------------------------------------------------- crop
        g_cr = QGroupBox("Crop  (full-resolution pixels)")
        grid = QGridLayout(g_cr)
        grid.setSpacing(5)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.spins = {}
        for i, key in enumerate(("x", "y", "w", "h")):
            lab = QLabel(key.upper())
            sp = QSpinBox()
            sp.setRange(-200000, 400000)
            sp.setSingleStep(1)
            sp.setKeyboardTracking(False)
            # narrow enough for two label+box pairs to sit side by side in
            # the right-hand column, which is where this panel lives now
            sp.setMinimumWidth(64)
            sp.valueChanged.connect(self._crop_spin)
            self.spins[key] = sp
            grid.addWidget(lab, i // 2, (i % 2) * 2)
            grid.addWidget(sp, i // 2, (i % 2) * 2 + 1)

        self.cmb_aspect = QComboBox()
        self.cmb_aspect.addItems(list(ASPECTS.keys()))
        self.cmb_aspect.currentTextChanged.connect(self._aspect)
        grid.addWidget(QLabel("Lock"), 2, 0)
        grid.addWidget(self.cmb_aspect, 2, 1, 1, 3)

        b0 = QPushButton("Draw a new crop  (C)")
        b0.setToolTip("Arms the crop tool for one drag, then releases it")
        b0.clicked.connect(self.requestCropTool.emit)
        b1 = QPushButton("Fit to page edges")
        b1.setToolTip("Detect the sheet and set crop + skew from it (E)")
        b1.clicked.connect(self.requestFitToPage.emit)
        b2 = QPushButton("Whole page")
        b2.clicked.connect(self.requestSelectAll.emit)
        b3 = QPushButton("Clear crop")
        b3.clicked.connect(self.requestResetCrop.emit)
        grid.addWidget(b0, 3, 0, 1, 4)
        grid.addWidget(b1, 4, 0, 1, 4)
        grid.addWidget(b2, 5, 0, 1, 2)
        grid.addWidget(b3, 5, 2, 1, 2)

        self.cmb_cropmode = QComboBox()
        self.cmb_cropmode.addItems(list(CROP_MODES.keys()))
        self.cmb_cropmode.setToolTip(
            "What \"crop all pages\" does to this page.\n"
            "Spread-aligned pages are locked automatically so their framing "
            "cannot be overwritten.")
        self.cmb_cropmode.currentTextChanged.connect(self._cropmode)
        grid.addWidget(QLabel("This page"), 7, 0)
        grid.addWidget(self.cmb_cropmode, 7, 1, 1, 3)
        root.addWidget(g_cr)

        # --------------------------------------------------------- propagate
        g_ap = QGroupBox("Reuse this framing")
        va = QVBoxLayout(g_ap)
        va.setSpacing(4)
        bp = QPushButton("Copy framing from previous page")
        bp.clicked.connect(self.requestCopyPrevious.emit)
        bd = QPushButton("Apply to every page in this document")
        bd.clicked.connect(self.requestApplyToDocument.emit)
        bs = QPushButton("Apply to selected pages")
        bs.clicked.connect(self.requestApplyToSelection.emit)
        for b in (bp, bd, bs):
            b.setMinimumHeight(26)
            va.addWidget(b)
        root.addWidget(g_ap)

        # -------------------------------------------------------------- blur
        g_bl = QGroupBox("Blur")
        vb = QVBoxLayout(g_bl)
        vb.setSpacing(5)
        b_add = QPushButton("Blur a spot  (B)")
        b_add.setToolTip("Drag out a blur over something on the page")
        b_add.clicked.connect(self.requestBlurTool.emit)
        vb.addWidget(b_add)

        self.lbl_blur = QLabel("No blur selected")
        self.lbl_blur.setStyleSheet("color:#9aa0aa;")
        vb.addWidget(self.lbl_blur)

        rowb = QHBoxLayout()
        self.cmb_blur_mode = QComboBox()
        self.cmb_blur_mode.addItems(["Blur", "Pixelate"])
        self.cmb_blur_mode.currentTextChanged.connect(self._blur_mode)
        rowb.addWidget(QLabel("Style"))
        rowb.addWidget(self.cmb_blur_mode, 1)
        vb.addLayout(rowb)

        rows = QHBoxLayout()
        self.sld_blur = QSlider(Qt.Horizontal)
        self.sld_blur.setRange(2, 300)
        self.sld_blur.setToolTip("How strong the blur is, in source pixels")
        self.sld_blur.valueChanged.connect(self._blur_strength)
        self.sp_blur = QSpinBox()
        self.sp_blur.setRange(2, 300)
        self.sp_blur.setKeyboardTracking(False)
        self.sp_blur.valueChanged.connect(self._blur_strength_spin)
        rows.addWidget(QLabel("Strength"))
        rows.addWidget(self.sld_blur, 1)
        rows.addWidget(self.sp_blur)
        vb.addLayout(rows)

        self.btn_blur_del = QPushButton("Delete this blur  (Del)")
        self.btn_blur_del.clicked.connect(self.requestBlurDelete.emit)
        vb.addWidget(self.btn_blur_del)
        root.addWidget(g_bl)

        # ------------------------------------------------------------ output
        g_out = QGroupBox("Result")
        fo = QFormLayout(g_out)
        self.lbl_out_px = QLabel("-")
        self.lbl_out_in = QLabel("-")
        mono = QFont("Consolas"); mono.setStyleHint(QFont.Monospace)
        self.lbl_out_px.setFont(mono)
        self.lbl_out_in.setFont(mono)
        fo.addRow("Pixels", self.lbl_out_px)
        fo.addRow("Physical", self.lbl_out_in)
        self.chk_done = QCheckBox("Marked done")
        self.chk_done.toggled.connect(self._done)
        fo.addRow("", self.chk_done)
        root.addWidget(g_out)

        root.addStretch(1)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.set_page(None)

    # ------------------------------------------------------------------ load
    def set_page(self, edit: Optional[PageEdit]) -> None:
        self.edit = edit
        enabled = edit is not None
        for w in self.findChildren(QWidget):
            if w is not self.lbl_name and w is not self.lbl_src:
                w.setEnabled(enabled)
        if edit is None:
            self.lbl_name.setText("No page selected")
            self.lbl_src.setText("Open a folder of scans to begin")
            return

        self._loading = True
        try:
            self.lbl_name.setText(edit.name)
            self.lbl_src.setText(
                f"{edit.src_w} × {edit.src_h} px · {edit.dpi:g} dpi · "
                f"{edit.dtype} · {edit.channels}ch")
            self._load_crop()
            self.chk_done.setChecked(edit.done)
            self.cmb_cropmode.setCurrentText(
                _MODE_LABEL.get(edit.crop_mode, "Takes a crop-all"))
            self.cmb_cropmode.setEnabled(True)
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

    def refresh(self) -> None:
        """Re-read values from the edit after the canvas changed something."""
        if self.edit is None:
            return
        self._loading = True
        try:
            self._load_crop()
        finally:
            self._loading = False
        self.refresh_output()

    def refresh_output(self) -> None:
        if self.edit is None:
            self.lbl_out_px.setText("-")
            self.lbl_out_in.setText("-")
            return
        w, h = self.edit.output_size()
        iw, ih = self.edit.output_inches()
        self.lbl_out_px.setText(f"{w} × {h}")
        self.lbl_out_in.setText(f"{iw:.3f} × {ih:.3f} in   "
                                f"({iw * 25.4:.1f} × {ih * 25.4:.1f} mm)")

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

    def set_blur(self, index: int, blur: Optional[list]) -> None:
        """Show the properties of the blur the canvas has selected."""
        self._loading = True
        try:
            on = blur is not None
            for w in (self.cmb_blur_mode, self.sld_blur, self.sp_blur,
                      self.btn_blur_del):
                w.setEnabled(on)
            if not on:
                self.lbl_blur.setText("No blur selected")
                return
            self.lbl_blur.setText(
                f"Blur {index + 1} · {int(blur[2])} × {int(blur[3])} px")
            radius = int(round(blur[4] if len(blur) > 4 else 8))
            self.sld_blur.setValue(max(2, min(300, radius)))
            self.sp_blur.setValue(max(2, min(300, radius)))
            self.cmb_blur_mode.setCurrentText(blur[5] if len(blur) > 5 else "Blur")
        finally:
            self._loading = False

    def _blur_strength(self, val: int) -> None:
        if self._loading:
            return
        self.sp_blur.blockSignals(True)
        self.sp_blur.setValue(val)
        self.sp_blur.blockSignals(False)
        self.blurChanged.emit(float(val), self.cmb_blur_mode.currentText())

    def _blur_strength_spin(self, val: int) -> None:
        if self._loading:
            return
        self.sld_blur.blockSignals(True)
        self.sld_blur.setValue(val)
        self.sld_blur.blockSignals(False)
        self.blurChanged.emit(float(val), self.cmb_blur_mode.currentText())

    def _blur_mode(self, mode: str) -> None:
        if self._loading:
            return
        self.blurChanged.emit(float(self.sp_blur.value()), mode)

    def _done(self, on: bool) -> None:
        if self._loading:
            return
        self.doneToggled.emit(on)

    def _cropmode(self, label: str) -> None:
        if self._loading:
            return
        self.cropModeChanged.emit(CROP_MODES.get(label, "linked"))
