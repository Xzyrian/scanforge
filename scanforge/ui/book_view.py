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
from . import theme

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
        self._sizes: Optional[list] = None
        self._metrics: Optional[tuple] = None
        self._turn_t = 0.0                # 0..1 through the turn
        self._turn_dir = 0                # +1 forward, -1 back, 0 at rest
        self._turn_from = 0
        self._turn_to = 0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(420)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._anim.valueChanged.connect(self._on_turn)
        self._anim.finished.connect(self._turn_done)

        # how far the book has slid off the spine to centre a lone last page:
        # 0 = hinged at the spine, 1 = centred in the window
        self._slide = 0.0
        self._slide_ref: Optional[int] = None
        self._slide_anim = QVariantAnimation(self)
        self._slide_anim.setDuration(420)
        self._slide_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._slide_anim.valueChanged.connect(self._on_slide)

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

        # base_preview, not proxycache: a joined page's `path` is only its
        # first scan, and reading that stretched it across the whole joined
        # width.  It assembles joins (of joins) from their scans.
        arr = rnd.base_preview(page, want)
        tier = want
        if arr is None:
            arr = rnd.base_preview(page, "proxy")
            tier = "proxy"
            if want == "detail" and page.uid not in self._detail_wanted:
                self._detail_wanted.add(page.uid)
                for path in page.source_paths():
                    self.needDetail.emit(path)
        if arr is None:
            return None
        # The preview is stored unrotated, so its scale has to be measured
        # against the unrotated width.  base_size() is the width *after* a
        # 90-degree turn, which gave a turned page the wrong scale.
        rw, _rh = page.raw_size()
        scale = arr.shape[1] / float(rw) if rw else 1.0
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
        return h * self._scale() > proxycache.PROXY * 0.55

    def invalidate(self) -> None:
        """Forget everything derived from the pages - sizes included, because a
        crop changes how much room the document needs."""
        self._pix.clear()
        self._detail_wanted.clear()
        self._sizes = None
        self._metrics = None
        self._sync_slide(animate=False)
        self.update()

    # ---------------------------------------------------------------- layout
    def _page_sizes(self) -> list:
        if self._sizes is None:
            self._sizes = [(max(1, w), max(1, h))
                           for w, h in (pg.output_size()
                                        for pg in self.project.pages)]
        return self._sizes

    def _doc_metrics(self) -> tuple:
        """How much room the *whole document* needs, at one scale.

        Every spread is drawn at the same scale, chosen so the largest of them
        fits.  Scaling each spread to its own size would make the book breathe
        as you turn - a smaller page swelling to fill the window - and would
        hide the very thing this view is for, which is whether the pages agree
        with each other.
        """
        if self._metrics is None:
            sizes = self._page_sizes()
            need_w = 1.0
            need_h = 1.0
            for li, ri in self.spreads():
                lw = sizes[li][0] if li is not None else 0
                rw = sizes[ri][0] if ri is not None else 0
                # the spine sits in the middle of the window, so half the width
                # is all a page ever gets - including a cover standing on its
                # own on the right, which is why this is not conditional
                need_w = max(need_w, 2 * max(lw, rw))
                for idx in (li, ri):
                    if idx is not None:
                        need_h = max(need_h, sizes[idx][1])
            self._metrics = (need_w, need_h)
        return self._metrics

    def _scale(self) -> float:
        need_w, need_h = self._doc_metrics()
        avail_w = max(80, self.width() - 56)
        avail_h = max(80, self.height() - 56)
        return min(avail_w / need_w, avail_h / need_h) * self.zoom

    def _lone_left(self, spread: int) -> Optional[int]:
        """The page index of a spread that is a single page on the left - the
        back cover of a book whose page count runs out on a left-hand page."""
        sp = self.spreads()
        if not sp:
            return None
        li, ri = sp[max(0, min(spread, len(sp) - 1))]
        return li if (li is not None and ri is None) else None

    def _slide_px(self) -> float:
        """How far the book is currently pushed off the spine.

        Only a lone *last* page does this.  The front cover stays where a book
        puts it - on the right of the spine, its left edge against the gutter,
        because the whole of the block is still under it.  The back cover has
        nothing left under it, so the book eases across until that page sits in
        the middle of the window.  It travels; it does not jump.
        """
        if self._slide <= 1e-3 or self._slide_ref is None:
            return 0.0
        sizes = self._page_sizes()
        if not (0 <= self._slide_ref < len(sizes)):
            return 0.0
        return self._slide * sizes[self._slide_ref][0] * self._scale() / 2.0

    def spine_x(self) -> float:
        return self.width() / 2.0 + self.pan.x() + self._slide_px()

    def _rects_for(self, spread: int) -> tuple:
        """Where the two pages of a spread sit, and which pages they are.

        Everything hangs off the spine: a left page ends at it, a right page
        starts at it, and a page on its own keeps the side of the spine it
        belongs to - page one on the right, the way a book falls open.  The
        book as a whole may be slid sideways (see `_slide_px`), which moves the
        spine and the pages with it, together.
        """
        sp = self.spreads()
        if not sp:
            return (QRectF(), QRectF(), None, None)
        li, ri = sp[max(0, min(spread, len(sp) - 1))]
        sizes = self._page_sizes()
        s = self._scale()
        cy = self.height() / 2.0 + self.pan.y()

        def box(idx):
            if idx is None:
                return (0.0, 0.0)
            w, h = sizes[idx]
            return (w * s, h * s)

        lw, lh = box(li)
        rw, rh = box(ri)
        x = self.spine_x()
        lr = QRectF(x - lw, cy - lh / 2, lw, lh) if lw else QRectF()
        rr = QRectF(x, cy - rh / 2, rw, rh) if rw else QRectF()
        return lr, rr, li, ri

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

        if self._turning:
            self._paint_turn(p)
        else:
            lr, rr, li, ri = self._rects_for(self.spread)
            self._paint_page(p, lr, li)
            self._paint_page(p, rr, ri)
            self._paint_gutter(p, lr, rr)
            self._paint_numbers(p, ((lr, li), (rr, ri)))

    def _paint_page(self, p: QPainter, rect: QRectF, index) -> None:
        if rect.width() <= 0:
            return
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(rect.adjusted(4, 5, 4, 5), 3, 3)
        p.setBrush(PAPER)
        p.drawRect(rect)
        pm = self._page_pixmap(index)
        if pm is not None:
            p.drawPixmap(rect.toRect(), pm)
        elif index is not None:
            p.setPen(QColor("#9aa0aa"))
            p.drawText(rect, Qt.AlignCenter, "reading…")

    def _paint_gutter(self, p: QPainter, lr: QRectF, rr: QRectF) -> None:
        """The shadow where the pages meet, so a spread reads as one sheet."""
        if lr.width() <= 0 or rr.width() <= 0:
            return
        x = lr.right()
        top, h = int(min(lr.top(), rr.top())), int(max(lr.height(), rr.height()))
        p.setPen(Qt.NoPen)
        for i in range(14):
            a = int(58 * (1 - i / 14.0))
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRect(int(x - i - 1), top, 1, h)
            p.drawRect(int(x + i), top, 1, h)

    def _paint_turn(self, p: QPainter) -> None:
        """A leaf swinging about the spine, with the page printed on it.

        A turn is one physical sheet: its front is the page you were reading
        and its back is the page you are turning to.  So the first half of the
        movement closes the old page down onto the spine, and the second half
        opens the new one out of it - and what is underneath is already the
        destination, which is why the far page appears as the leaf uncovers it.

        The sheet is squeezed horizontally rather than projected in three
        dimensions.  That is what the width of a turning page does as it comes
        up, and it costs one drawPixmap rather than a mesh.
        """
        t = max(0.0, min(1.0, self._turn_t))
        fwd = self._turn_dir > 0
        fl, fr, fli, fri = self._rects_for(self._turn_from)
        tl, tr, tli, tri = self._rects_for(self._turn_to)

        # what sits still underneath the moving leaf
        if fwd:
            base_left, base_left_i = fl, fli
            base_right, base_right_i = tr, tri
            first_rect, first_i = fr, fri        # closing down onto the spine
            second_rect, second_i = tl, tli      # opening out of it
        else:
            base_left, base_left_i = tl, tli
            base_right, base_right_i = fr, fri
            first_rect, first_i = fl, fli
            second_rect, second_i = tr, tri

        self._paint_page(p, base_left, base_left_i)
        self._paint_page(p, base_right, base_right_i)
        self._paint_gutter(p, base_left, base_right)

        if t < 0.5:
            frac = 1.0 - t * 2.0                 # full width down to nothing
            rect, idx, closing = first_rect, first_i, True
        else:
            frac = (t - 0.5) * 2.0               # nothing back up to full
            rect, idx, closing = second_rect, second_i, False
        if rect.width() <= 0:
            return

        hinge_right = (fwd and closing) or (not fwd and not closing)
        w = max(1.0, rect.width() * frac)
        x = self.spine_x() if hinge_right else self.spine_x() - w
        leaf = QRectF(x, rect.top(), w, rect.height())

        pm = self._page_pixmap(idx)
        p.setPen(Qt.NoPen)
        if frac < 0.98:            # at rest the leaf sits flat on the page
            p.setBrush(QColor(0, 0, 0, 80))
            p.drawRect(leaf.adjusted(2, 3, 2, 3))
        p.setBrush(PAPER)
        p.drawRect(leaf)
        if pm is not None:
            p.drawPixmap(leaf.toRect(), pm)

        # the sheet darkens as it stands up, and its free edge catches a line
        shade = int(78 * (1.0 - frac))
        if shade:
            p.setBrush(QColor(0, 0, 0, shade))
            p.drawRect(leaf)
        p.setPen(QPen(QColor(0, 0, 0, 90), 1))
        edge = leaf.topLeft() if hinge_right else leaf.topRight()
        edge2 = leaf.bottomLeft() if hinge_right else leaf.bottomRight()
        p.drawLine(edge, edge2)

    def _paint_numbers(self, p: QPainter, pairs) -> None:
        f = QFont(p.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 0.5))
        p.setFont(f)
        p.setPen(QColor(150, 156, 166))
        for rect, idx in pairs:
            if rect.width() <= 0 or idx is None:
                continue
            p.drawText(QRectF(rect.left(), rect.bottom() + 6, rect.width(), 18),
                       Qt.AlignHCenter | Qt.AlignTop, str(idx + 1))

    # ------------------------------------------------------------ navigation
    @property
    def _turning(self) -> bool:
        return self._turn_dir != 0

    def go(self, spread: int, animate: bool = True) -> None:
        n = self.count()
        if not n:
            return
        spread = max(0, min(n - 1, spread))
        if spread == self.spread:
            return
        if self._turning:
            self._anim.stop()
            self._turn_dir = 0
        direction = 1 if spread > self.spread else -1
        was = self.spread
        self.spread = spread
        self.spreadChanged.emit(spread)
        if animate and abs(spread - was) == 1:
            self._turn_from, self._turn_to = was, spread
            self._turn_dir = direction
            self._turn_t = 0.0
            self._anim.setStartValue(0.0)
            self._anim.setEndValue(1.0)
            self._anim.start()
            self._sync_slide(animate=True)
        else:
            self._sync_slide(animate=False)
            self.update()

    def _sync_slide(self, animate: bool = True) -> None:
        """Bring the book to where the open spread wants it to sit."""
        idx = self._lone_left(self.spread)
        if idx is not None:
            self._slide_ref = idx
        target = 1.0 if idx is not None else 0.0
        if abs(target - self._slide) < 1e-3:
            self._slide_anim.stop()
            self._slide = target
            return
        self._slide_anim.stop()
        if not animate:
            self._slide = target
            self.update()
            return
        self._slide_anim.setStartValue(float(self._slide))
        self._slide_anim.setEndValue(target)
        self._slide_anim.start()

    def _on_slide(self, value) -> None:
        self._slide = float(value)
        self.update()

    def next_spread(self) -> None:
        self.go(self.spread + 1)

    def prev_spread(self) -> None:
        self.go(self.spread - 1)

    def _on_turn(self, value) -> None:
        self._turn_t = float(value)
        self.update()

    def _turn_done(self) -> None:
        self._turn_t = 0.0
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
            self.go(0, animate=False)
        elif k == Qt.Key_End:
            self.go(self.count() - 1, animate=False)
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
        if ev.button() != Qt.LeftButton:
            return
        # Click a page to turn it: the left page goes back, the right page
        # goes on.  The split is the spine itself, so the half you click is
        # the half you are looking at.
        lr, rr, _li, _ri = self._rects_for(self.spread)
        split = lr.right() if lr.width() else self.spine_x()
        if ev.position().x() < split:
            self.prev_spread()
        else:
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
        theme.hint(self.lbl)
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
        self.view._metrics = None          # the pairing decides the widest spread
        self.slider.setMaximum(max(0, self.view.count() - 1))
        self.view.spread = max(0, min(self.view.spread, self.view.count() - 1))
        self.view._sync_slide(animate=False)
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
