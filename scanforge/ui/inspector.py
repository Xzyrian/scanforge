"""The right-hand controls: orientation, straightening, crop numbers, output size."""

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
    "Follows the shared crop": "linked",
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
    angleChanged = Signal(float)
    cropChanged = Signal(float, float, float, float)
    orientationChanged = Signal(int, bool, bool)     # quadrant, flip_h, flip_v
    requestAutoDeskew = Signal()
    requestFitToPage = Signal()
    requestStraightenTool = Signal()
    requestCropTool = Signal()
    requestBlurTool = Signal()
    requestBlurDelete = Signal()
    blurChanged = Signal(float, str)      # radius, mode
    requestResetCrop = Signal()
    linkToggled = Signal(bool)
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
        self.setMinimumWidth(310)
        self.setMaximumWidth(380)

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

        # ------------------------------------------------------- orientation
        g_or = QGroupBox("Orientation")
        h = QHBoxLayout(g_or)
        h.setSpacing(4)
        self.btn_ccw = QPushButton("⟲ 90°")
        self.btn_cw = QPushButton("⟳ 90°")
        self.btn_fh = QPushButton("Flip ⇄")
        self.btn_fv = QPushButton("Flip ⇅")
        for b in (self.btn_ccw, self.btn_cw, self.btn_fh, self.btn_fv):
            b.setMinimumHeight(26)
            h.addWidget(b)
        self.btn_ccw.clicked.connect(lambda: self._quad(-1))
        self.btn_cw.clicked.connect(lambda: self._quad(1))
        self.btn_fh.clicked.connect(lambda: self._flip(True))
        self.btn_fv.clicked.connect(lambda: self._flip(False))
        root.addWidget(g_or)

        # -------------------------------------------------------- straighten
        g_st = QGroupBox("Straighten")
        v = QVBoxLayout(g_st)
        v.setSpacing(6)

        row = QHBoxLayout()
        self.spin_angle = QDoubleSpinBox()
        self.spin_angle.setRange(-45.0, 45.0)
        self.spin_angle.setDecimals(3)
        self.spin_angle.setSingleStep(0.01)
        self.spin_angle.setSuffix(" °")
        self.spin_angle.setMinimumWidth(96)
        self.spin_angle.setKeyboardTracking(False)
        self.spin_angle.valueChanged.connect(self._angle_spin)
        row.addWidget(QLabel("Angle"))
        row.addWidget(self.spin_angle)
        self.btn_angle_reset = QPushButton("0")
        self.btn_angle_reset.setFixedWidth(28)
        self.btn_angle_reset.setToolTip("Reset rotation")
        self.btn_angle_reset.clicked.connect(lambda: self.spin_angle.setValue(0.0))
        row.addWidget(self.btn_angle_reset)
        row.addStretch(1)
        v.addLayout(row)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(-2000, 2000)          # ±2.000° at 0.001 resolution
        self.slider.setSingleStep(10)
        self.slider.setPageStep(100)
        self.slider.setTracking(True)
        self.slider.valueChanged.connect(self._angle_slider)
        v.addWidget(self.slider)

        ticks = QHBoxLayout()
        for lbl in ("−2°", "−1°", "0", "1°", "2°"):
            t = QLabel(lbl)
            t.setAlignment(Qt.AlignCenter)
            t.setStyleSheet("color:#767c86;font-size:10px;")
            ticks.addWidget(t)
        v.addLayout(ticks)

        nudge = QHBoxLayout()
        for text, delta in (("−0.10", -0.10), ("−0.05", -0.05), ("−0.01", -0.01),
                            ("+0.01", 0.01), ("+0.05", 0.05), ("+0.10", 0.10)):
            b = QPushButton(text)
            b.setMinimumHeight(24)
            b.clicked.connect(lambda _=False, d=delta: self._nudge_angle(d))
            nudge.addWidget(b)
        v.addLayout(nudge)

        tools = QHBoxLayout()
        self.btn_line = QPushButton("Guide line")
        self.btn_line.setToolTip(
            "Lay a straightedge across the page and leave it there, then turn "
            "the page onto it with the nudges above (L)")
        self.btn_line.clicked.connect(self.requestStraightenTool.emit)
        self.btn_auto = QPushButton("Auto-deskew")
        self.btn_auto.setToolTip("Estimate skew from the text lines (D)")
        self.btn_auto.clicked.connect(self.requestAutoDeskew.emit)
        tools.addWidget(self.btn_line)
        tools.addWidget(self.btn_auto)
        v.addLayout(tools)
        root.addWidget(g_st)

        # -------------------------------------------------------------- crop
        g_cr = QGroupBox("Crop  (full-resolution pixels)")
        grid = QGridLayout(g_cr)
        grid.setSpacing(5)
        self.spins = {}
        for i, key in enumerate(("x", "y", "w", "h")):
            lab = QLabel(key.upper())
            sp = QSpinBox()
            sp.setRange(-200000, 400000)
            sp.setSingleStep(1)
            sp.setKeyboardTracking(False)
            sp.setMinimumWidth(88)
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

        self.chk_link = QCheckBox("Same crop for every page")
        self.chk_link.setToolTip(
            "On by default: one crop for the whole project, so every exported "
            "page is identical in size")
        self.chk_link.setChecked(True)
        self.chk_link.toggled.connect(self._link)
        grid.addWidget(self.chk_link, 6, 0, 1, 4)

        self.cmb_cropmode = QComboBox()
        self.cmb_cropmode.addItems(list(CROP_MODES.keys()))
        self.cmb_cropmode.setToolTip(
            "How this page follows the shared crop.\n"
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
            self.spin_angle.setValue(edit.angle)
            self.slider.setValue(int(round(max(-2.0, min(2.0, edit.angle)) * 1000)))
            self._load_crop()
            self.chk_done.setChecked(edit.done)
            self.cmb_cropmode.setCurrentText(
                _MODE_LABEL.get(edit.crop_mode, "Follows the shared crop"))
            self.cmb_cropmode.setEnabled(self.chk_link.isChecked())
        finally:
            self._loading = False
        self.refresh_output()

    def set_link(self, on: bool) -> None:
        self._loading = True
        try:
            self.chk_link.setChecked(on)
            self.cmb_cropmode.setEnabled(on)
        finally:
            self._loading = False

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
            self.spin_angle.setValue(self.edit.angle)
            self.slider.setValue(
                int(round(max(-2.0, min(2.0, self.edit.angle)) * 1000)))
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
    def _angle_spin(self, val: float) -> None:
        if self._loading:
            return
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(max(-2.0, min(2.0, val)) * 1000)))
        self.slider.blockSignals(False)
        self.angleChanged.emit(round(val, 3))

    def _angle_slider(self, val: int) -> None:
        if self._loading:
            return
        a = val / 1000.0
        self.spin_angle.blockSignals(True)
        self.spin_angle.setValue(a)
        self.spin_angle.blockSignals(False)
        self.angleChanged.emit(a)

    def _nudge_angle(self, d: float) -> None:
        self.spin_angle.setValue(round(self.spin_angle.value() + d, 3))

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

    def _quad(self, direction: int) -> None:
        if self.edit is None:
            return
        q = (self.edit.quadrant + direction) % 4
        self.orientationChanged.emit(q, self.edit.flip_h, self.edit.flip_v)

    def _flip(self, horizontal: bool) -> None:
        if self.edit is None:
            return
        fh = not self.edit.flip_h if horizontal else self.edit.flip_h
        fv = self.edit.flip_v if horizontal else not self.edit.flip_v
        self.orientationChanged.emit(self.edit.quadrant, fh, fv)

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

    def _link(self, on: bool) -> None:
        self.cmb_cropmode.setEnabled(on)
        if self._loading:
            return
        self.linkToggled.emit(on)

    def _cropmode(self, label: str) -> None:
        if self._loading:
            return
        self.cropModeChanged.emit(CROP_MODES.get(label, "linked"))
