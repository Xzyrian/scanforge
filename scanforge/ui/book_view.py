"""Read the document as a book, to check it before it goes out.

Everything else in this app looks at one page at a time, which is the right way
to crop and straighten but the wrong way to answer "is this actually right?".
Facing pages that are each perfectly square can still disagree with each other;
a page can be upside down, out of order, or missing, and nothing about the
single-page view makes that obvious.  So this shows the finished pages the way
a reader meets them - two at a time, in order, turning.

What is drawn is the *rendered* page: orientation, straightening, crop and
blurs all applied, exactly as the exporter will write it.  It reads the proxy
tier for that, and swaps in the detail tier when you zoom far enough in for the
difference to show, which is the same bargain the editing canvas makes.
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np
from PySide6.QtCore import (QEasingCurve, QPoint, QRect, QRectF, QSize, Qt,
                            QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QKeySequence, QPainter, QPen,
                           QPixmap, QShortcut)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout,
                               QLabel, QPushButton, QSlider, QVBoxLayout,
                               QWidget)

from .. import proxycache, render as rnd
from ..model import PageEdit, Project
from .canvas import numpy_to_pixmap
from .loader import Background

PAPER = QColor(250, 250, 248)
DESK = QColor(26, 27, 30)


def spreads_for(count: int, cover_alone: bool = True) -> list:
    """Pair the pages the way a book falls open.

    A bound document opens with page one on the right, on its own, because its
    back is the inside of the cover.  Everything after that pairs up.  Turn
    that off for something that was never bound - a folded leaflet, a set of
    loose sheets - and the pairs start from the first page.
    """
    if count <= 0:
        return []
    out = []
    if cover_alone:
        out.append((None, 0))
        i = 1
    else:
        i = 0
    while i < count:
        left = i
        right = i + 1 if i + 1 < count else None
        out.append((left, right))
        i += 2
    return out


class BookView(QWidget):
    """Paints the open spread, and the turn between two of them."""

    spreadChanged = Signal(int)
    statusMessage = Signal(str)
    needDetail = Signal(str)              # path whose detail tier would help

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.setMinimumSize(560, 420)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

        self.cover_alone = True
        self.spread = 0
        self.zoom = 1.0                   # 1.0 = fit the window
        self.pan = QPoint(0, 0)
        self._drag: Optional[QPoint] = None

        self._pix: dict = {}              # uid -> (QPixmap, tier)
        self._detail_wanted: set = set()
        self._turn = 0.0                  # -1..1 while a page is turning
        self._turn_dir = 0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._anim.valueChanged.connect(self._on_turn)
        self._anim.finished.connect(self._turn_done)

    # ------------------------------------------------------------- structure
    def spreads(self) -> list:
        return spreads_for(len(self.project.pages), self.cover_alone)

    def count(self) -> int:
        return len(self.spreads())

    def current(self) -> tuple:
        sp = self.spreads()
        if not sp:
            return (None, None)
        return sp[max(0, min(self.spread, len(sp) - 1))]

    # ------------------------------------------------------------- rendering
    def _page_pixmap(self, index: Optional[int]) -> Optional[QPixmap]:
        """The finished page, as it will be exported."""
        if index is None or not (0 <= index < len(self.project.pages)):
            return None
        page = self.project.pages[index]
        want = "detail" if self._detail_useful(page) else "proxy"
        got = self._pix.get(page.uid)
        if got is not None and got[1] == want and got[2] == self._stamp(page):
            return got[0]

        arr = proxycache.load(page.path, want, build_if_missing=False)
        tier = want
        if arr is None:
            arr = proxycache.load(page.path, "proxy", build_if_missing=False)
            tier = "proxy"
            if want == "detail" and page.uid not in self._detail_wanted:
                self._detail_wanted.add(page.uid)
                self.needDetail.emit(page.path)
        if arr is None:
            return None
        bw, _bh = page.base_size()
        scale = arr.shape[1] / float(bw) if bw else 1.0
        try:
            out = rnd.render(page, arr, interp="linear", source_scale=scale)
        except Exception:                              # noqa: BLE001
            return None
        pm = numpy_to_pixmap(out)
        self._pix[page.uid] = (pm, tier, self._stamp(page))
        if len(self._pix) > 12:                        # a few spreads' worth
            for uid in list(self._pix)[:-12]:
                self._pix.pop(uid, None)
        return pm

    @staticmethod
    def _stamp(page: PageEdit) -> tuple:
        """Everything about a page that changes how it renders."""
        return (page.quadrant, page.flip_h, page.flip_v, round(page.angle, 4),
                tuple(page.crop) if page.crop else None, len(page.blurs))

    def _detail_useful(self, page: PageEdit) -> bool:
        """True once the page is drawn bigger than the proxy has pixels for."""
        if self.zoom < 1.4:
            return False
        w, h = page.output_size()
        if not w:
            return False
        drawn = self._page_height() * (w / float(h or 1))
        return drawn > proxycache.PROXY * 0.55

    def invalidate(self) -> None:
        self._pix.clear()
        self._detail_wanted.clear()
        self.update()

    # ---------------------------------------------------------------- layout
    def _page_height(self) -> float:
        return max(40.0, (self.height() - 48) * self.zoom)

    def _spread_rects(self) -> tuple:
        """Where the two pages sit, in widget coordinates."""
        li, ri = self.current()
        lh = self._page_height()
        lpm, rpm = self._page_pixmap(li), self._page_pixmap(ri)

        def wide(pm):
            if pm is None or pm.height() == 0:
                return lh * 0.72
            return lh * pm.width() / pm.height()

        lw = wide(lpm) if lpm is not None else 0.0
        rw = wide(rpm) if rpm is not None else 0.0
        total = lw + rw
        x = (self.width() - total) / 2.0 + self.pan.x()
        y = (self.height() - lh) / 2.0 + self.pan.y()
        lr = QRectF(x, y, lw, lh) if lw else QRectF()
        rr = QRectF(x + lw, y, rw, lh) if rw else QRectF()
        return lr, rr, lpm, rpm

    # ----------------------------------------------------------------- paint
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), DESK)
        if not self.project.pages:
            p.setPen(QColor("#8a9099"))
            p.drawText(self.rect(), Qt.AlignCenter, "Nothing to read yet")
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)

        lr, rr, lpm, rpm = self._spread_rects()
        spine = (lr.right() if lr.width() else
                 (rr.left() if rr.width() else self.width() / 2.0))

        for rect, pm in ((lr, lpm), (rr, rpm)):
            if rect.width() <= 0:
                continue
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 70))
            p.drawRoundedRect(rect.adjusted(4, 5, 4, 5), 3, 3)
            p.setBrush(PAPER)
            p.drawRect(rect)
            if pm is not None:
                p.drawPixmap(rect.toRect(), pm)
            else:
                p.setPen(QColor("#9aa0aa"))
                p.drawText(rect, Qt.AlignCenter, "reading…")

        # the gutter shadow, so facing pages read as one sheet of paper
        if lr.width() and rr.width():
            grad = QRect(int(spine - 14), int(lr.top()), 28, int(lr.height()))
            p.setPen(Qt.NoPen)
            for i in range(14):
                a = int(58 * (1 - i / 14.0))
                p.setBrush(QColor(0, 0, 0, a))
                p.drawRect(int(spine - i - 1), grad.top(), 1, grad.height())
                p.drawRect(int(spine + i), grad.top(), 1, grad.height())

        if self._turn:
            self._paint_turn(p, lr, rr, spine)

        self._paint_numbers(p, lr, rr)

    def _paint_turn(self, p: QPainter, lr: QRectF, rr: QRectF,
                    spine: float) -> None:
        """The leaf standing up off the page while it turns.

        A page turn is a sheet rotating about the spine, and the honest cheap
        version of that in two dimensions is a horizontal squeeze: the leaf
        keeps its height and loses its width as it comes up, then gains it
        again coming down on the other side.  It reads as a turn without
        pretending to be three-dimensional.
        """
        t = abs(self._turn)
        forward = self._turn_dir > 0
        src = rr if forward else lr
        if src.width() <= 0:
            src = lr if forward else rr
        if src.width() <= 0:
            return
        w = src.width() * max(0.02, 1.0 - t)
        x = spine if forward else spine - w
        leaf = QRectF(x, src.top(), w, src.height())
        p.setPen(Qt.NoPen)
        p.setBrush(PAPER.darker(103 + int(t * 18)))
        p.drawRect(leaf)
        p.setPen(QPen(QColor(0, 0, 0, 60), 1))
        p.drawLine(leaf.topRight() if forward else leaf.topLeft(),
                   leaf.bottomRight() if forward else leaf.bottomLeft())

    def _paint_numbers(self, p: QPainter, lr: QRectF, rr: QRectF) -> None:
        li, ri = self.current()
        f = QFont(p.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 0.5))
        p.setFont(f)
        p.setPen(QColor(150, 156, 166))
        for rect, idx in ((lr, li), (rr, ri)):
            if rect.width() <= 0 or idx is None:
                continue
            p.drawText(QRectF(rect.left(), rect.bottom() + 6, rect.width(), 18),
                       Qt.AlignHCenter | Qt.AlignTop, str(idx + 1))

    # ------------------------------------------------------------ navigation
    def go(self, spread: int, animate: bool = True) -> None:
        n = self.count()
        if not n:
            return
        spread = max(0, min(n - 1, spread))
        if spread == self.spread:
            return
        direction = 1 if spread > self.spread else -1
        self.spread = spread
        self.spreadChanged.emit(spread)
        if animate and not self._anim.state():
            self._turn_dir = direction
            self._anim.setStartValue(0.0)
            self._anim.setEndValue(1.0)
            self._anim.start()
        else:
            self.update()

    def next_spread(self) -> None:
        self.go(self.spread + 1)

    def prev_spread(self) -> None:
        self.go(self.spread - 1)

    def _on_turn(self, value) -> None:
        self._turn = float(value) * (1 if self._turn_dir > 0 else -1)
        self.update()

    def _turn_done(self) -> None:
        self._turn = 0.0
        self._turn_dir = 0
        self.update()

    # ---------------------------------------------------------------- input
    def keyPressEvent(self, ev):
        k = ev.key()
        if k in (Qt.Key_Right, Qt.Key_PageDown, Qt.Key_Space):
            self.next_spread()
        elif k in (Qt.Key_Left, Qt.Key_PageUp, Qt.Key_Backspace):
            self.prev_spread()
        elif k == Qt.Key_Home:
            self.go(0)
        elif k == Qt.Key_End:
            self.go(self.count() - 1)
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self.set_zoom(self.zoom * 1.25)
        elif k == Qt.Key_Minus:
            self.set_zoom(self.zoom / 1.25)
        elif k == Qt.Key_0:
            self.set_zoom(1.0)
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MiddleButton or self.zoom > 1.01:
            self._drag = ev.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if ev.button() == Qt.LeftButton:
            # the outer thirds turn the page, the way clicking the edge of a
            # book does; the middle is left alone for panning and selection
            x = ev.position().x()
            if x < self.width() * 0.33:
                self.prev_spread()
            elif x > self.width() * 0.67:
                self.next_spread()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.pan += ev.position().toPoint() - self._drag
            self._drag = ev.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, _ev):
        self._drag = None
        self.unsetCursor()

    def wheelEvent(self, ev):
        d = ev.angleDelta().y()
        if not d:
            return
        if ev.modifiers() & Qt.ControlModifier:
            self.set_zoom(self.zoom * (1.0015 ** d))
        elif d < 0:
            self.next_spread()
        else:
            self.prev_spread()

    def set_zoom(self, z: float) -> None:
        z = max(1.0, min(6.0, z))
        if abs(z - self.zoom) < 1e-6:
            return
        self.zoom = z
        if z <= 1.0:
            self.pan = QPoint(0, 0)
        self.statusMessage.emit(f"Zoom {z * 100:.0f}%")
        self.update()


class BookWindow(QDialog):
    """The reading window: a book, a slider, and a way back."""

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preview — read as a book")
        self.setModal(False)
        self.resize(1180, 820)
        self.project = project
        self.bg = Background(workers=1, parent=self)
        self.bg.done.connect(self._detail_ready)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.view = BookView(project, self)
        root.addWidget(self.view, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_prev = QPushButton("‹  Back")
        self.btn_prev.clicked.connect(self.view.prev_spread)
        self.btn_next = QPushButton("Next  ›")
        self.btn_next.clicked.connect(self.view.next_spread)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.valueChanged.connect(
            lambda v: self.view.go(v, animate=False))
        self.lbl = QLabel("")
        self.lbl.setStyleSheet("color:#9aa0aa;")
        self.lbl.setMinimumWidth(150)

        self.chk_cover = QCheckBox("First page is a cover")
        self.chk_cover.setChecked(True)
        self.chk_cover.setToolTip(
            "A bound document opens with page one on its own, on the right. "
            "Turn this off for loose sheets or a folded leaflet.")
        self.chk_cover.toggled.connect(self._set_cover)

        self.cmb_zoom = QComboBox()
        self.cmb_zoom.addItems(["Fit", "150%", "200%", "300%", "400%"])
        self.cmb_zoom.currentIndexChanged.connect(self._set_zoom_choice)

        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_next)
        row.addWidget(self.slider, 1)
        row.addWidget(self.lbl)
        row.addWidget(self.chk_cover)
        row.addWidget(self.cmb_zoom)
        b_close = QPushButton("Close")
        b_close.clicked.connect(self.accept)
        row.addWidget(b_close)
        root.addLayout(row)

        self.view.spreadChanged.connect(self._refresh)
        self.view.statusMessage.connect(lambda t: self.lbl.setText(t))
        self.view.needDetail.connect(self._want_detail)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.accept)
        self.reload()

    # ------------------------------------------------------------------ wiring
    def reload(self) -> None:
        """Pick up edits made since the window was opened."""
        self.view.invalidate()
        self.slider.setMaximum(max(0, self.view.count() - 1))
        self._refresh(self.view.spread)

    def _refresh(self, spread: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(spread)
        self.slider.blockSignals(False)
        li, ri = self.view.current()
        pages = [str(i + 1) for i in (li, ri) if i is not None]
        self.lbl.setText(f"{' – '.join(pages)}  of {len(self.project.pages)}")
        self.btn_prev.setEnabled(spread > 0)
        self.btn_next.setEnabled(spread < self.view.count() - 1)
        self.view.update()

    def _set_cover(self, on: bool) -> None:
        self.view.cover_alone = on
        self.slider.setMaximum(max(0, self.view.count() - 1))
        self.view.spread = min(self.view.spread, self.view.count() - 1)
        self._refresh(self.view.spread)

    def _set_zoom_choice(self, i: int) -> None:
        self.view.set_zoom([1.0, 1.5, 2.0, 3.0, 4.0][max(0, min(4, i))])
        self.view.update()

    # The detail tier is a full decode, so it happens on a worker and the page
    # is redrawn when it lands - the same bargain the editing canvas makes.
    def _want_detail(self, path: str) -> None:
        self.bg.run(f"detail:{path}", self._detail_work, path)

    @staticmethod
    def _detail_work(path: str) -> str:
        proxycache.load(path, "detail")
        return path

    def _detail_ready(self, token: str, _result) -> None:
        if token.startswith("detail:"):
            self.view._pix.clear()
            self.view.update()

    def closeEvent(self, ev):
        self.bg.shutdown()
        super().closeEvent(ev)
