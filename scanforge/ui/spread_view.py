"""Spread alignment.

Two pages that were scanned separately are butted together at the gutter so you
can see them as the reader would, with shared guides running across both.  You
nudge and rotate each side until the baselines, rules and margins agree - and
then the two pages go back to being two independent files.  Nothing is ever
merged on disk; this view only writes each page's own crop and angle.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QGroupBox,
                               QHBoxLayout, QLabel, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from ..geometry import rotation_matrix, rotated_size
from ..model import PageEdit
from .canvas import numpy_to_pixmap, qtransform_from_affine, remap_crop_for_angle


class SpreadView(QWidget):
    """Paints both pages in one continuous coordinate space (proxy pixels)."""

    changed = Signal()
    activeChanged = Signal(str)          # "left" | "right"
    statusMessage = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(700, 460)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        self.left: Optional[PageEdit] = None
        self.right: Optional[PageEdit] = None
        self._pix: dict = {}
        self._ref: dict = {}
        self._scale: dict = {}

        self.active = "left"
        self.gap = 0
        self.show_beyond = True
        self.guides: list = []           # horizontal lines, spread-space y
        self.vguides: list = []
        self.link_vertical = False       # move both pages together

        self._zoom = 0.25
        self._pan = QPointF(30, 30)
        self._drag = None

    # ------------------------------------------------------------------ setup
    def set_pages(self, left: PageEdit, right: PageEdit,
                  left_proxy: np.ndarray, right_proxy: np.ndarray) -> None:
        self.left, self.right = left, right
        for side, edit, proxy in (("left", left, left_proxy),
                                  ("right", right, right_proxy)):
            pm = numpy_to_pixmap(proxy)
            self._pix[side] = pm
            self._ref[side] = (pm.width(), pm.height())
            bw, _bh = edit.base_size()
            self._scale[side] = pm.width() / float(bw) if bw else 1.0
            if edit.crop is None:
                W, H = rotated_size(pm.width(), pm.height(), edit.angle)
                s = self._scale[side]
                edit.crop = [0.0, 0.0, W / s, H / s]
        self.fit()

    def edit_for(self, side: str) -> Optional[PageEdit]:
        return self.left if side == "left" else self.right

    def crop_spread(self, side: str) -> QRectF:
        """The page's crop rectangle, in spread space (proxy pixels)."""
        e = self.edit_for(side)
        if e is None or e.crop is None:
            return QRectF()
        s = self._scale.get(side, 1.0)
        w, h = e.crop[2] * s, e.crop[3] * s
        if side == "left":
            return QRectF(0.0, 0.0, w, h)
        lw = 0.0
        if self.left is not None and self.left.crop:
            lw = self.left.crop[2] * self._scale.get("left", 1.0)
        return QRectF(lw + self.gap, 0.0, w, h)

    def spread_size(self) -> tuple[float, float]:
        a, b = self.crop_spread("left"), self.crop_spread("right")
        return (b.right() if b.width() else a.right(),
                max(a.height(), b.height()))

    # ------------------------------------------------------------------- view
    def fit(self) -> None:
        W, H = self.spread_size()
        if W <= 0 or H <= 0:
            return
        z = min((self.width() - 60) / W, (self.height() - 60) / H)
        self._zoom = max(0.02, z)
        self._pan = QPointF((self.width() - W * self._zoom) / 2.0,
                            (self.height() - H * self._zoom) / 2.0)
        self.update()

    def _to_screen(self, p: QPointF) -> QPointF:
        return QPointF(p.x() * self._zoom + self._pan.x(),
                       p.y() * self._zoom + self._pan.y())

    def _to_spread(self, p: QPointF) -> QPointF:
        return QPointF((p.x() - self._pan.x()) / self._zoom,
                       (p.y() - self._pan.y()) / self._zoom)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if not d:
            return
        before = self._to_spread(QPointF(ev.position()))
        self._zoom = max(0.02, min(8.0, self._zoom * (1.0015 ** d)))
        after = self._to_spread(QPointF(ev.position()))
        self._pan += (after - before) * self._zoom
        self.update()

    # ------------------------------------------------------------------ paint
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(32, 34, 38))
        if self.left is None or self.right is None:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, "Select two pages to align")
            return

        p.setRenderHint(QPainter.SmoothPixmapTransform, self._drag is None)
        p.translate(self._pan)
        p.scale(self._zoom, self._zoom)

        for side in ("left", "right"):
            self._draw_page(p, side)

        W, H = self.spread_size()
        px = 1.0 / self._zoom

        # gutter
        gx = self.crop_spread("left").right() + self.gap / 2.0
        p.setPen(QPen(QColor(255, 120, 120, 190), px * 1.5, Qt.DashLine))
        p.drawLine(QPointF(gx, -40 * px), QPointF(gx, H + 40 * px))

        # shared guides
        p.setPen(QPen(QColor(0, 220, 255, 220), px * 1.4))
        for y in self.guides:
            p.drawLine(QPointF(-60 * px, y), QPointF(W + 60 * px, y))
        for x in self.vguides:
            p.drawLine(QPointF(x, -60 * px), QPointF(x, H + 60 * px))

        # page frames, active one highlighted
        for side in ("left", "right"):
            r = self.crop_spread(side)
            if not r.width():
                continue
            on = side == self.active
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 200, 60) if on else QColor(150, 155, 165),
                          px * (2.2 if on else 1.2)))
            p.drawRect(r)

    def _draw_page(self, p: QPainter, side: str) -> None:
        e = self.edit_for(side)
        pm = self._pix.get(side)
        if e is None or pm is None:
            return
        frame = self.crop_spread(side)
        if not frame.width():
            return
        s = self._scale.get(side, 1.0)
        w, h = self._ref[side]
        M, _ = rotation_matrix(w, h, e.angle)
        cx, cy = e.crop[0] * s, e.crop[1] * s

        def blit(alpha: float):
            p.save()
            p.setOpacity(alpha)
            p.translate(frame.left() - cx, frame.top() - cy)
            p.setTransform(qtransform_from_affine(M), True)
            p.scale(w / float(pm.width()), h / float(pm.height()))
            p.drawPixmap(0, 0, pm)
            p.restore()

        if self.show_beyond:
            p.save()
            p.setClipRect(frame.adjusted(-frame.width() * 0.35, -frame.height() * 0.2,
                                         frame.width() * 0.35, frame.height() * 0.2))
            blit(0.22)
            p.restore()

        p.save()
        p.setClipRect(frame)
        blit(1.0)
        p.restore()

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, ev):
        pos = QPointF(ev.position())
        sp = self._to_spread(pos)
        if ev.button() == Qt.MiddleButton:
            self._drag = ("pan", pos)
            return
        if ev.button() != Qt.LeftButton:
            return
        tol = 6.0 / self._zoom
        for i, y in enumerate(self.guides):
            if abs(sp.y() - y) <= tol:
                self._drag = ("guide", i)
                return
        for i, x in enumerate(self.vguides):
            if abs(sp.x() - x) <= tol:
                self._drag = ("vguide", i)
                return
        for side in ("left", "right"):
            if self.crop_spread(side).contains(sp):
                if side != self.active:
                    self.active = side
                    self.activeChanged.emit(side)
                self._drag = ("move", (sp, list(self.edit_for(side).crop), side))
                self.update()
                return
        self._drag = ("pan", pos)

    def mouseMoveEvent(self, ev):
        pos = QPointF(ev.position())
        sp = self._to_spread(pos)
        if self._drag is None:
            return
        kind, data = self._drag
        if kind == "pan":
            self._pan += pos - data
            self._drag = ("pan", pos)
            self.update()
            return
        if kind == "guide":
            self.guides[data] = sp.y()
            self.update()
            return
        if kind == "vguide":
            self.vguides[data] = sp.x()
            self.update()
            return
        if kind == "move":
            start, orig, side = data
            s = self._scale.get(side, 1.0)
            dx = (sp.x() - start.x()) / s
            dy = (sp.y() - start.y()) / s
            e = self.edit_for(side)
            # dragging the paper moves the window over it, hence the sign flip
            e.crop = [orig[0] - dx, orig[1] - dy, orig[2], orig[3]]
            if self.link_vertical:
                other = self.edit_for("right" if side == "left" else "left")
                if other is not None and other.crop:
                    so = self._scale.get("right" if side == "left" else "left", 1.0)
                    if not hasattr(self, "_link_orig"):
                        self._link_orig = list(other.crop)
                    other.crop[1] = self._link_orig[1] - dy * s / so
            self.statusMessage.emit(
                f"{side} page offset  Δx {-dx:+.0f} px   Δy {-dy:+.0f} px")
            self.changed.emit()
            self.update()

    def mouseReleaseEvent(self, ev):
        self._drag = None
        if hasattr(self, "_link_orig"):
            del self._link_orig
        self.update()
        self.changed.emit()

    # --------------------------------------------------------------- keyboard
    def keyPressEvent(self, ev):
        e = self.edit_for(self.active)
        if e is None or e.crop is None:
            return
        step = 10.0 if ev.modifiers() & Qt.ShiftModifier else 1.0
        k = ev.key()
        moved = True
        if k == Qt.Key_Left:
            e.crop[0] += step
        elif k == Qt.Key_Right:
            e.crop[0] -= step
        elif k == Qt.Key_Up:
            e.crop[1] += step
        elif k == Qt.Key_Down:
            e.crop[1] -= step
        elif k == Qt.Key_BracketLeft:
            self.rotate_active(-0.01 if not (ev.modifiers() & Qt.ShiftModifier) else -0.1)
            return
        elif k == Qt.Key_BracketRight:
            self.rotate_active(0.01 if not (ev.modifiers() & Qt.ShiftModifier) else 0.1)
            return
        elif k == Qt.Key_Tab:
            self.active = "right" if self.active == "left" else "left"
            self.activeChanged.emit(self.active)
        else:
            moved = False
            super().keyPressEvent(ev)
        if moved:
            self.changed.emit()
            self.update()

    def rotate_active(self, delta: float) -> None:
        e = self.edit_for(self.active)
        if e is None:
            return
        old = e.angle
        new = round(old + delta, 3)
        remap_crop_for_angle(e, old, new)
        e.angle = new
        self.changed.emit()
        self.update()

    # ------------------------------------------------------------------- ops
    def add_guide(self, horizontal: bool = True) -> None:
        W, H = self.spread_size()
        if horizontal:
            self.guides.append(H / 2.0 + len(self.guides) * 40)
        else:
            self.vguides.append(W / 2.0)
        self.update()

    def clear_guides(self) -> None:
        self.guides.clear()
        self.vguides.clear()
        self.update()

    def match_size(self, dimension: str = "height") -> None:
        """Give both pages identical output dimensions, driven by the active one."""
        a = self.edit_for(self.active)
        b = self.edit_for("right" if self.active == "left" else "left")
        if a is None or b is None or not a.crop or not b.crop:
            return
        if dimension in ("height", "both"):
            b.crop[3] = a.crop[3]
        if dimension in ("width", "both"):
            b.crop[2] = a.crop[2]
        self.changed.emit()
        self.update()

    def match_angle(self) -> None:
        a = self.edit_for(self.active)
        b = self.edit_for("right" if self.active == "left" else "left")
        if a is None or b is None:
            return
        old = b.angle
        remap_crop_for_angle(b, old, a.angle)
        b.angle = a.angle
        self.changed.emit()
        self.update()

    def align_tops(self) -> None:
        """Make the two crops start at the same distance down their own page."""
        a = self.edit_for(self.active)
        b = self.edit_for("right" if self.active == "left" else "left")
        if a and b and a.crop and b.crop:
            b.crop[1] = a.crop[1]
            self.changed.emit()
            self.update()


class SpreadPane(QWidget):
    """Facing-page alignment, in the main working area.

    This used to be a floating window, which put the alignment somewhere other
    than where the work happens: the queue and the inspector were behind it,
    the canvas underneath was showing a page you were not looking at, and
    closing it was a separate decision from finishing the job.  It takes the
    canvas's place instead, so aligning is a mode of the editor rather than a
    detour out of it.
    """

    applied = Signal()
    joinRequested = Signal(int)          # gap in proxy-independent full-res px
    finished = Signal()                  # leave the aligner, back to the canvas

    def __init__(self, left: PageEdit, right: PageEdit,
                 left_proxy: np.ndarray, right_proxy: np.ndarray, parent=None,
                 join: bool = False):
        super().__init__(parent)
        self.left, self.right = left, right
        self._join_mode = join
        self._before = (dict(left.__dict__), dict(right.__dict__))

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        head = QLabel("Join two scans into one page" if join
                      else "Align facing pages")
        f = head.font()
        f.setPointSizeF(f.pointSizeF() + 1.5)
        f.setBold(True)
        head.setFont(f)
        head.setStyleSheet("color:#e6e9ee;")
        root.addWidget(head)

        self.view = SpreadView(self)
        self.view.set_pages(left, right, left_proxy, right_proxy)
        root.addWidget(self.view, 1)

        self.status = QLabel("Drag either page to slide it under its crop · "
                             "Tab switches sides · [ ] rotate by 0.01°")
        self.status.setStyleSheet("color:#9aa0aa;")
        root.addWidget(self.status)
        self.view.statusMessage.connect(self.status.setText)

        root.addLayout(self._controls())
        self.view.changed.connect(self._refresh)
        self.view.activeChanged.connect(lambda _s: self._refresh())
        self._refresh()

    def _controls(self) -> QHBoxLayout:
        bar = QHBoxLayout()

        g_ang = QGroupBox("Angle")
        ha = QHBoxLayout(g_ang)
        self.sp_left = QDoubleSpinBox()
        self.sp_right = QDoubleSpinBox()
        for sp, side in ((self.sp_left, "left"), (self.sp_right, "right")):
            sp.setRange(-45, 45)
            sp.setDecimals(3)
            sp.setSingleStep(0.01)
            sp.setSuffix(" °")
            sp.setKeyboardTracking(False)
            sp.valueChanged.connect(lambda v, s=side: self._set_angle(s, v))
            ha.addWidget(QLabel("L" if side == "left" else "R"))
            ha.addWidget(sp)
        b_match_a = QPushButton("Match")
        b_match_a.setToolTip("Copy the active page's angle to the other page")
        b_match_a.clicked.connect(self.view.match_angle)
        ha.addWidget(b_match_a)
        bar.addWidget(g_ang)

        g_guide = QGroupBox("Guides")
        hg = QHBoxLayout(g_guide)
        b1 = QPushButton("Add ―")
        b1.clicked.connect(lambda: self.view.add_guide(True))
        b2 = QPushButton("Add │")
        b2.clicked.connect(lambda: self.view.add_guide(False))
        b3 = QPushButton("Clear")
        b3.clicked.connect(self.view.clear_guides)
        for b in (b1, b2, b3):
            hg.addWidget(b)
        bar.addWidget(g_guide)

        g_size = QGroupBox("Match sizes")
        hs = QHBoxLayout(g_size)
        for label, dim in (("Height", "height"), ("Width", "width"), ("Both", "both")):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, d=dim: self.view.match_size(d))
            hs.addWidget(b)
        b_top = QPushButton("Align tops")
        b_top.clicked.connect(self.view.align_tops)
        hs.addWidget(b_top)
        bar.addWidget(g_size)

        g_view = QGroupBox("View")
        hv = QHBoxLayout(g_view)
        self.sp_gap = QSpinBox()
        self.sp_gap.setRange(0, 400)
        self.sp_gap.setSuffix(" px gap")
        self.sp_gap.valueChanged.connect(self._set_gap)
        hv.addWidget(self.sp_gap)
        self.chk_beyond = QCheckBox("Show beyond crop")
        self.chk_beyond.setChecked(True)
        self.chk_beyond.toggled.connect(self._set_beyond)
        hv.addWidget(self.chk_beyond)
        self.chk_link = QCheckBox("Link vertical")
        self.chk_link.setToolTip("Slide both pages up and down together")
        self.chk_link.toggled.connect(lambda v: setattr(self.view, "link_vertical", v))
        hv.addWidget(self.chk_link)
        b_fit = QPushButton("Fit")
        b_fit.clicked.connect(self.view.fit)
        hv.addWidget(b_fit)
        bar.addWidget(g_view)

        bar.addStretch(1)
        self.lbl_sizes = QLabel()
        self.lbl_sizes.setStyleSheet("color:#9aa0aa;")
        bar.addWidget(self.lbl_sizes)

        b_cancel = QPushButton("Revert")
        b_cancel.clicked.connect(self._revert)
        self.b_join = QPushButton("Join into one page")
        self.b_join.setToolTip(
            "Make these two scans a single page. They are butted together "
            "exactly as shown, so trim each crop's inner edge first to cut the "
            "seam off. The source files are not modified, and the page can be "
            "split apart again later.")
        self.b_join.clicked.connect(self._join)
        b_ok = QPushButton("Keep alignment")
        b_ok.clicked.connect(self._accept)
        (self.b_join if self._join_mode else b_ok).setDefault(True)
        bar.addWidget(b_cancel)
        bar.addWidget(self.b_join)
        bar.addWidget(b_ok)
        return bar

    # ------------------------------------------------------------------ slots
    def _set_angle(self, side: str, val: float) -> None:
        e = self.view.edit_for(side)
        if e is None or abs(e.angle - val) < 1e-9:
            return
        old = e.angle
        remap_crop_for_angle(e, old, val)
        e.angle = round(val, 3)
        self.view.update()
        self._refresh()

    def _set_gap(self, v: int) -> None:
        self.view.gap = v
        self.view.update()

    def _set_beyond(self, on: bool) -> None:
        self.view.show_beyond = on
        self.view.update()

    def _refresh(self) -> None:
        for sp, e in ((self.sp_left, self.left), (self.sp_right, self.right)):
            sp.blockSignals(True)
            sp.setValue(e.angle)
            sp.blockSignals(False)
        lw, lh = self.left.output_size()
        rw, rh = self.right.output_size()
        diff = lh - rh
        note = "same height" if diff == 0 else f"heights differ by {abs(diff)} px"
        self.lbl_sizes.setText(f"L {lw}×{lh}   R {rw}×{rh}   ({note})")

    def _revert(self) -> None:
        for e, before in ((self.left, self._before[0]), (self.right, self._before[1])):
            e.__dict__.update(before)
        self.view.update()
        self.applied.emit()
        self.finished.emit()

    def _join(self) -> None:
        """Hand the two aligned pages over to be assembled into one.

        The gap shown on screen is in proxy pixels; the assembled page is built
        at full resolution, so it is converted here rather than at the far end
        where the proxy scale is no longer in view.
        """
        s = self.view._scale.get("left", 1.0) or 1.0
        self.joinRequested.emit(int(round(self.view.gap / s)))
        self.finished.emit()

    def _accept(self) -> None:
        self.left.spread_partner = self.right.uid
        self.right.spread_partner = self.left.uid
        self.left.spread_side, self.right.spread_side = "left", "right"
        # Lock both crops.  Their framing was set against each other, so the
        # project-wide crop must not come along later and overwrite it - which
        # is exactly what used to happen, silently, the next time any other
        # page's crop was touched.
        self.left.crop_mode = self.right.crop_mode = "locked"
        self.applied.emit()
        self.finished.emit()


# The aligner used to be a floating dialog; anything still asking for one gets
# the pane, which is the same thing in the place it belongs.
SpreadWindow = SpreadPane
