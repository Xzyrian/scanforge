"""The editing canvas.

Everything you see here is drawn from a ~2200px proxy with a Qt transform, so
dragging a crop handle or scrubbing the rotation dial costs a few milliseconds
regardless of whether the source is 4 megapixels or 140.  The scene coordinate
system *is* the rotated frame, which means the crop rectangle you draw needs no
translation at all to become the crop the exporter applies.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QImage, QPainter, QPen,
                           QPixmap, QTransform)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from ..geometry import (Rect, apply_affine, invert_affine, rotation_matrix,
                        rotated_size)
from ..model import PageEdit

HANDLE_PX = 9.0
EDGE_PX = 7.0

# The inch grid, as (colour, width in screen pixels).  It was one hairline at
# alpha 55, which against a scan is the same as not being drawn at all - and a
# measuring aid you cannot see is worse than none, because you think you
# checked.  Named rather than inlined so it can be measured.
GRID_PEN = (QColor(0, 255, 200, 110), 2.0)


class Tool(str, Enum):
    """
    CURSOR is the resting state: pan, zoom, and adjust an existing crop by its
    handles.  It cannot start a new rectangle, so a stray click on the page can
    never wipe out framing you already set.

    CROP, CROP_ALL, BLUR and CUT are one-shot - they do their thing once and
    hand control back to CURSOR, which is why you have to arm them deliberately.

    CROP and CROP_ALL draw the same rectangle; they differ only in how far it
    reaches.  Cropping one page and cropping the whole document are different
    intentions, and asking for them with different tools is more honest than a
    mode toggle that silently changes what the next drag means.
    """
    CURSOR = "cursor"
    CROP = "crop"
    CROP_ALL = "crop_all"
    BLUR = "blur"
    CUT = "cut"
    PAN = "pan"

    # kept so older projects and any external callers still resolve
    SELECT = "cursor"


def qtransform_from_affine(M: np.ndarray) -> QTransform:
    return QTransform(M[0, 0], M[1, 0],
                      M[0, 1], M[1, 1],
                      M[0, 2], M[1, 2])


def numpy_to_pixmap(arr: np.ndarray) -> QPixmap:
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    arr = np.ascontiguousarray(arr[:, :, :3])
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, 3 * w, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(img)


def remap_crop_for_angle(edit: PageEdit, old_angle: float, new_angle: float) -> None:
    """Keep the crop and any blur regions over the same content when the
    straightening angle moves."""
    w, h = edit.base_size()
    if not w or not h:
        return
    if edit.crop is None and not edit.blurs:
        return
    Mo, _ = rotation_matrix(w, h, old_angle)
    Mn, _ = rotation_matrix(w, h, new_angle)
    Moi = invert_affine(Mo)

    def move(cx, cy):
        src = apply_affine(Moi, [[cx, cy]])[0]
        return apply_affine(Mn, [src])[0]

    if edit.crop is not None:
        r = edit.crop_rect()
        cx, cy = r.center()
        dst = move(cx, cy)
        edit.crop = [float(dst[0] - r.w / 2.0), float(dst[1] - r.h / 2.0),
                     float(r.w), float(r.h)]

    for b in edit.blurs:
        dst = move(b[0] + b[2] / 2.0, b[1] + b[3] / 2.0)
        b[0] = float(dst[0] - b[2] / 2.0)
        b[1] = float(dst[1] - b[3] / 2.0)


class Canvas(QGraphicsView):
    editChanged = Signal()            # crop / angle moved; inspector should refresh
    statusMessage = Signal(str)
    cursorMoved = Signal(float, float)   # position in full-res source pixels
    interactionStarted = Signal(str)     # open an undo group with this label
    interactionEnded = Signal()          # close it
    toolFinished = Signal()              # a one-shot tool has done its job
    blurSelected = Signal(int)           # index into edit.blurs, or -1
    # axis ("v"/"h") and the fractions along the crop to cut at.  A measured
    # cut is one request carrying several positions, not several requests: the
    # second one would otherwise land on a page the first had already replaced.
    cutRequested = Signal(str, object)
    zoomChanged = Signal(float)          # percent of full-resolution pixels
    cropDrawn = Signal(bool)             # a crop was drawn; True = every page
    pageChanged = Signal()               # a page arrived, or the canvas emptied
    filesDropped = Signal(list)          # scans or a project dropped on the page

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setBackgroundBrush(QColor(38, 40, 44))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setAcceptDrops(True)

        self.edit: Optional[PageEdit] = None
        self._pix: Optional[QPixmap] = None
        self._proxy_arr: Optional[np.ndarray] = None   # kept for blur previews
        self._blur_pix: dict = {}        # (mode, radius bucket) -> QPixmap
        self.blur_selected: int = -1     # index into edit.blurs
        self._ref_size = (1, 1)          # proxy dimensions = scene reference
        self._scale = 1.0                # proxy px per full-res px

        self.tool = Tool.CURSOR
        self.show_grid = False
        self.grid_inches = 0.5
        self.dim_outside = True
        self.guides_h: list = []         # scene y
        self.guides_v: list = []         # scene x
        self.lock_aspect: Optional[float] = None

        self._drag = None                # (kind, data)
        self._cut = None                 # (axis, scene position) while placing
        self._drew_for_all = False       # the crop being drawn is for every page
        self._fast_draw = False
        self._overlay_pages: list = []   # extra (pixmap, transform, rect) for spread view
        self._message = "Open a folder of scans to begin"

    # ------------------------------------------------------------------ setup
    def set_message(self, text: str) -> None:
        """Show a centred note instead of a page - used while a proxy decodes."""
        self._message = text
        self._pix = None
        self.viewport().update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self._pix is None and self._message:
            p = QPainter(self.viewport())
            p.setPen(QColor(138, 144, 153))
            f = p.font()
            f.setPointSize(11)
            p.setFont(f)
            p.drawText(self.viewport().rect(), Qt.AlignCenter, self._message)
            p.end()

    def set_page(self, edit: Optional[PageEdit], proxy: Optional[np.ndarray]) -> None:
        self.edit = edit
        if edit is None or proxy is None:
            self._pix = None
            self.scene().setSceneRect(QRectF(0, 0, 1, 1))
            self.viewport().update()
            self.pageChanged.emit()
            return
        self._message = ""
        self._pix = numpy_to_pixmap(proxy)
        self._proxy_arr = proxy
        self._blur_pix.clear()
        self.blur_selected = -1
        self._ref_size = (self._pix.width(), self._pix.height())
        bw, bh = edit.base_size()
        self._scale = self._ref_size[0] / float(bw) if bw else 1.0
        self._update_scene_rect()
        self.fit()
        self.pageChanged.emit()

    def set_detail_pixmap(self, arr: Optional[np.ndarray]) -> None:
        """Swap in a higher-resolution tier without changing scene coordinates."""
        if arr is None or self.edit is None:
            return
        self._pix = numpy_to_pixmap(arr)
        self.viewport().update()

    def _update_scene_rect(self) -> None:
        if self.edit is None:
            return
        w, h = self._ref_size
        W, H = rotated_size(w, h, self.edit.angle)
        pad = max(W, H) * 0.6
        self.scene().setSceneRect(QRectF(-pad, -pad, W + 2 * pad, H + 2 * pad))

    def page_canvas(self) -> tuple[int, int]:
        w, h = self._ref_size
        return rotated_size(w, h, self.edit.angle if self.edit else 0.0)

    # ----------------------------------------------------------------- blurs
    def _blurred_pixmap(self, mode: str, radius_full: float) -> Optional[QPixmap]:
        """A blurred copy of the whole proxy, cached per (mode, radius).

        Drawing it clipped to a blur rectangle gives a true preview of what the
        exporter will produce, rather than a grey box standing in for one.
        """
        if self._proxy_arr is None:
            return None
        r = max(2.0, radius_full * (self._scale or 1.0))
        key = (mode, round(r / 2.0) * 2)      # bucket, so a drag reuses one copy
        pm = self._blur_pix.get(key)
        if pm is None:
            from .. import render as rnd
            arr = np.ascontiguousarray(self._proxy_arr.copy())
            h, w = arr.shape[:2]
            rnd.blur_region(arr, 0, 0, w, h, key[1] or 2, mode)
            pm = numpy_to_pixmap(arr)
            if len(self._blur_pix) > 6:
                self._blur_pix.clear()
            self._blur_pix[key] = pm
        return pm

    def blur_scene(self, i: int) -> QRectF:
        b = self.edit.blurs[i]
        s = self._scale or 1.0
        return QRectF(b[0] * s, b[1] * s, b[2] * s, b[3] * s)

    def set_blur_scene(self, i: int, rect: QRectF) -> None:
        s = self._scale or 1.0
        r = rect.normalized()
        b = self.edit.blurs[i]
        b[0], b[1] = r.x() / s, r.y() / s
        b[2], b[3] = max(4.0, r.width() / s), max(4.0, r.height() / s)
        self.viewport().update()

    def add_blur(self, rect: QRectF) -> int:
        s = self._scale or 1.0
        r = rect.normalized()
        w, h = max(8.0, r.width() / s), max(8.0, r.height() / s)
        radius = max(4.0, min(w, h) * 0.18)
        self.edit.blurs.append([r.x() / s, r.y() / s, w, h, radius, "Blur"])
        self.blur_selected = len(self.edit.blurs) - 1
        return self.blur_selected

    def delete_blur(self, i: int = -1) -> bool:
        if self.edit is None or not self.edit.blurs:
            return False
        i = self.blur_selected if i < 0 else i
        if not (0 <= i < len(self.edit.blurs)):
            return False
        self.interactionStarted.emit("Delete blur")
        del self.edit.blurs[i]
        self.blur_selected = min(i, len(self.edit.blurs) - 1)
        self.interactionEnded.emit()
        self.editChanged.emit()
        self.viewport().update()
        return True

    def set_blur_property(self, radius: Optional[float] = None,
                          mode: Optional[str] = None) -> None:
        i = self.blur_selected
        if self.edit is None or not (0 <= i < len(self.edit.blurs)):
            return
        b = self.edit.blurs[i]
        if radius is not None:
            b[4] = max(2.0, radius)
        if mode is not None:
            b[5] = mode
        self.viewport().update()
        self.editChanged.emit()

    def _hit_blur(self, pos: QPointF):
        """(index, handle) for a blur under the cursor, handles taking priority."""
        if self.edit is None or not self.edit.blurs:
            return -1, None
        px = 1.0 / (self.view_scale() or 1.0)
        tol = HANDLE_PX * px
        order = ([self.blur_selected] if 0 <= self.blur_selected < len(self.edit.blurs)
                 else []) + [i for i in range(len(self.edit.blurs))
                             if i != self.blur_selected]
        for i in order:
            r = self.blur_scene(i)
            for j, (hx, hy) in enumerate(self._handle_points(r)):
                if abs(pos.x() - hx) <= tol and abs(pos.y() - hy) <= tol:
                    return i, self._HANDLE_NAMES[j]
            if r.contains(pos):
                return i, "move"
        return -1, None

    # ------------------------------------------------------- coordinate helpers
    def crop_scene(self) -> QRectF:
        """Crop rectangle in scene (proxy-scale rotated frame) coordinates."""
        W, H = self.page_canvas()
        if self.edit is None or self.edit.crop is None:
            return QRectF(0, 0, W, H)
        r = self.edit.crop_rect().scaled(self._scale)
        return QRectF(r.x, r.y, r.w, r.h)

    def set_crop_scene(self, rect: QRectF, commit: bool = True) -> None:
        if self.edit is None:
            return
        s = self._scale or 1.0
        r = rect.normalized()
        self.edit.crop = [r.x() / s, r.y() / s, max(1.0, r.width() / s),
                          max(1.0, r.height() / s)]
        if commit:
            self.editChanged.emit()
        self.viewport().update()

    def view_scale(self) -> float:
        return math.sqrt(abs(self.transform().determinant())) or 1.0

    def scene_to_source(self, p: QPointF) -> tuple[float, float]:
        """Scene point -> full-resolution source pixel (before rotation)."""
        if self.edit is None:
            return (0.0, 0.0)
        w, h = self._ref_size
        M, _ = rotation_matrix(w, h, self.edit.angle)
        src = apply_affine(invert_affine(M), [[p.x(), p.y()]])[0]
        s = self._scale or 1.0
        return (src[0] / s, src[1] / s)

    # ------------------------------------------------------------------ zoom
    def fit(self) -> None:
        if self.edit is None or self._pix is None:
            return
        W, H = self.page_canvas()
        if W <= 0:
            return
        self.resetTransform()
        vw, vh = self.viewport().width() - 24, self.viewport().height() - 24
        z = min(vw / W, vh / H) if W and H else 1.0
        self.scale(z, z)
        self.centerOn(W / 2.0, H / 2.0)
        self._announce_zoom()

    def zoom_to(self, factor_of_source: float) -> None:
        """1.0 means one screen pixel per full-resolution source pixel."""
        if self.edit is None or self._pix is None:
            return
        target = factor_of_source * self._scale
        cur = self.view_scale()
        if cur > 0:
            self.scale(target / cur, target / cur)
        self._announce_zoom()

    def zoom_by(self, f: float, anchor: Optional[QPointF] = None) -> None:
        # There is nothing to make bigger before a page is open, and a view
        # transform applied to an empty scene sticks: the first page then
        # arrives at whatever magnification the empty canvas was left at.
        if self.edit is None or self._pix is None:
            return
        if anchor is None:
            anchor = QPointF(self.viewport().rect().center())
        before = self.mapToScene(anchor.toPoint())
        self.scale(f, f)
        after = self.mapToScene(anchor.toPoint())
        d = after - before
        self.translate(d.x(), d.y())
        self._announce_zoom()

    def zoom_percent(self) -> float:
        """Where the view sits, as a percentage of full-resolution pixels."""
        return 100.0 * self.view_scale() / (self._scale or 1.0)

    def set_zoom_percent(self, pct: float, anchor=None) -> None:
        cur = self.zoom_percent()
        if cur <= 0 or pct <= 0:
            return
        self.zoom_by(pct / cur, anchor)

    def _announce_zoom(self) -> None:
        self.zoomChanged.emit(self.zoom_percent())

    def wheelEvent(self, ev):
        """Ctrl zooms, Shift scrolls across, and a bare wheel scrolls down.

        The wheel used to zoom on its own, which is the one binding that makes a
        long page hard to read: every attempt to move down it changed the
        magnification instead, and there is no way to scroll by accident.
        """
        delta = ev.angleDelta().y() or ev.angleDelta().x()
        if not delta or self.edit is None or self._pix is None:
            return
        mods = ev.modifiers()
        if mods & Qt.ControlModifier:
            self.zoom_by(1.0015 ** delta, QPointF(ev.position()))
            ev.accept()
            return
        bar = (self.horizontalScrollBar() if mods & Qt.ShiftModifier
               else self.verticalScrollBar())
        # a notch is 120 eighths of a degree; three lines is the usual step
        bar.setValue(bar.value() - int(round(delta / 120.0 * 3 * bar.singleStep()
                                             or delta / 4.0)))
        ev.accept()

    # ------------------------------------------------------------------ paint
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, self.backgroundBrush())
        if self._pix is None or self.edit is None:
            return
        w, h = self._ref_size
        M, (W, H) = rotation_matrix(w, h, self.edit.angle)

        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, not self._fast_draw)
        # paper drop shadow so the page reads against the workspace
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 60))
        painter.drawRect(QRectF(6, 6, W, H))
        painter.setTransform(qtransform_from_affine(M), True)
        sx = w / float(self._pix.width())
        sy = h / float(self._pix.height())
        painter.scale(sx, sy)
        painter.drawPixmap(0, 0, self._pix)
        painter.restore()

        # Blur regions are painted by clipping a pre-blurred copy of the same
        # proxy to each rectangle, so what you see is what gets exported.
        for b in (self.edit.blurs or []):
            pm = self._blurred_pixmap(b[5] if len(b) > 5 else "Blur",
                                      b[4] if len(b) > 4 else 8.0)
            if pm is None:
                continue
            s = self._scale or 1.0
            painter.save()
            painter.setClipRect(QRectF(b[0] * s, b[1] * s, b[2] * s, b[3] * s))
            painter.setRenderHint(QPainter.SmoothPixmapTransform, not self._fast_draw)
            painter.setTransform(qtransform_from_affine(M), True)
            painter.scale(w / float(pm.width()), h / float(pm.height()))
            painter.drawPixmap(0, 0, pm)
            painter.restore()

        for pix, tr, _ in self._overlay_pages:
            painter.save()
            painter.setTransform(tr, True)
            painter.drawPixmap(0, 0, pix)
            painter.restore()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        if self.edit is None or self._pix is None:
            return
        vs = self.view_scale() or 1.0
        px = 1.0 / vs                                  # one screen pixel, in scene units
        crop = self.crop_scene()
        W, H = self.page_canvas()

        if self.dim_outside and self.edit.crop is not None:
            outside = QColor(20, 21, 24, 150)
            full = rect
            painter.setPen(Qt.NoPen)
            painter.setBrush(outside)
            painter.drawRect(QRectF(full.left(), full.top(), full.width(),
                                    crop.top() - full.top()))
            painter.drawRect(QRectF(full.left(), crop.bottom(), full.width(),
                                    full.bottom() - crop.bottom()))
            painter.drawRect(QRectF(full.left(), crop.top(),
                                    crop.left() - full.left(), crop.height()))
            painter.drawRect(QRectF(crop.right(), crop.top(),
                                    full.right() - crop.right(), crop.height()))

        if self.show_grid:
            self._draw_grid(painter, crop, px)

        # guides
        gpen = QPen(QColor(0, 210, 255, 200), px * 1.2)
        painter.setPen(gpen)
        for y in self.guides_h:
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        for x in self.guides_v:
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

        # page outline
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(120, 125, 135), px))
        painter.drawRect(QRectF(0, 0, W, H))

        # crop frame + handles
        painter.setPen(QPen(QColor(255, 255, 255, 230), px * 1.6))
        painter.drawRect(crop)
        if self.tool in (Tool.CURSOR, Tool.CROP):
            hs = HANDLE_PX * px
            painter.setBrush(QBrush(QColor(255, 255, 255)))
            painter.setPen(QPen(QColor(30, 30, 30), px))
            for cx, cy in self._handle_points(crop):
                painter.drawRect(QRectF(cx - hs / 2, cy - hs / 2, hs, hs))

        # blur outlines, so an obscured spot is still findable and adjustable
        for i, b in enumerate(self.edit.blurs or []):
            r = self.blur_scene(i)
            on = i == self.blur_selected
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(120, 200, 255, 235 if on else 150),
                                px * (2.0 if on else 1.2), Qt.SolidLine))
            painter.drawRect(r)
            if on and self.tool in (Tool.CURSOR, Tool.BLUR):
                hs = HANDLE_PX * px
                painter.setBrush(QBrush(QColor(120, 200, 255)))
                painter.setPen(QPen(QColor(20, 30, 40), px))
                for cx, cy in self._handle_points(r):
                    painter.drawRect(QRectF(cx - hs / 2, cy - hs / 2, hs, hs))

        self._draw_cut(painter, crop, px)

    def _draw_cut(self, painter: QPainter, crop: QRectF, px: float) -> None:
        if self._cut is None:
            return
        axis, v = self._cut
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(255, 90, 90), px * 2.4))
        if axis == "v":
            painter.drawLine(QPointF(v, crop.top()), QPointF(v, crop.bottom()))
        else:
            painter.drawLine(QPointF(crop.left(), v), QPointF(crop.right(), v))

    def _draw_grid(self, painter: QPainter, crop: QRectF, px: float) -> None:
        if self.edit is None:
            return
        dpi = self.edit.dpi or 600.0
        step = self.grid_inches * dpi * self._scale
        if step < 6:
            return
        colour, width = GRID_PEN
        painter.setPen(QPen(colour, px * width))
        x = crop.left()
        while x < crop.right():
            painter.drawLine(QPointF(x, crop.top()), QPointF(x, crop.bottom()))
            x += step
        y = crop.top()
        while y < crop.bottom():
            painter.drawLine(QPointF(crop.left(), y), QPointF(crop.right(), y))
            y += step

    @staticmethod
    def _handle_points(r: QRectF):
        cx, cy = r.center().x(), r.center().y()
        return [(r.left(), r.top()), (cx, r.top()), (r.right(), r.top()),
                (r.right(), cy), (r.right(), r.bottom()), (cx, r.bottom()),
                (r.left(), r.bottom()), (r.left(), cy)]

    _HANDLE_NAMES = ["tl", "t", "tr", "r", "br", "b", "bl", "l"]
    _HANDLE_CURSORS = [Qt.SizeFDiagCursor, Qt.SizeVerCursor, Qt.SizeBDiagCursor,
                       Qt.SizeHorCursor, Qt.SizeFDiagCursor, Qt.SizeVerCursor,
                       Qt.SizeBDiagCursor, Qt.SizeHorCursor]

    def _hit(self, scene_pos: QPointF):
        crop = self.crop_scene()
        # A page with no crop of its own still shows a crop window - the whole
        # sheet - and its edges are draggable, because that is how cropping a
        # scan starts: pull the frame in from the edges.  What such a page must
        # *not* offer is "move", which would claim every click on the page and
        # leave the crop tool no way to draw a fresh rectangle inside it.
        blank = self.edit is not None and self.edit.crop is None
        px = 1.0 / (self.view_scale() or 1.0)
        tol = HANDLE_PX * px
        for i, (hx, hy) in enumerate(self._handle_points(crop)):
            if abs(scene_pos.x() - hx) <= tol and abs(scene_pos.y() - hy) <= tol:
                return self._HANDLE_NAMES[i], i
        gt = EDGE_PX * px
        for y in self.guides_h:
            if abs(scene_pos.y() - y) <= gt:
                return "guide_h", y
        for x in self.guides_v:
            if abs(scene_pos.x() - x) <= gt:
                return "guide_v", x
        if not blank and crop.adjusted(-tol, -tol, tol, tol).contains(scene_pos):
            return "move", None
        return None, None

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, ev):
        pos = self.mapToScene(ev.position().toPoint())
        if ev.button() == Qt.MiddleButton or self.tool == Tool.PAN:
            self._drag = ("pan", [QPointF(ev.position()), QPointF(0.0, 0.0)])
            self._fast_draw = True
            self.setCursor(Qt.ClosedHandCursor)
            return
        if ev.button() != Qt.LeftButton:
            return super().mousePressEvent(ev)

        if self.tool == Tool.CUT:
            self._cut = self._cut_at(pos, ev.modifiers())
            self._drag = ("cut", None)
            self.viewport().update()
            return

        if self.tool == Tool.BLUR:
            self.interactionStarted.emit("Add blur")
            i = self.add_blur(QRectF(pos, pos))
            self._fast_draw = True
            self._drag = ("blur_new", (i, pos))
            self.blurSelected.emit(i)
            return

        # A blur under the cursor takes priority over the crop: it sits on top
        # of the page, and it is the smaller, more fiddly target of the two.
        bi, bhandle = self._hit_blur(pos)
        if bi >= 0:
            if bi != self.blur_selected:
                self.blur_selected = bi
                self.blurSelected.emit(bi)
            self.interactionStarted.emit("Move blur" if bhandle == "move"
                                         else "Resize blur")
            self._fast_draw = True
            self._drag = ("blur_move" if bhandle == "move" else "blur_resize",
                          (bi, bhandle, self.blur_scene(bi), pos))
            self.viewport().update()
            return
        if self.blur_selected != -1:
            self.blur_selected = -1
            self.blurSelected.emit(-1)
            self.viewport().update()

        kind, data = self._hit(pos)
        # An armed crop tool draws a new rectangle rather than sliding the one
        # that is there - but a grab on a handle is not ambiguous, in either
        # tool.  Pulling the frame in from the edge of the sheet is how a scan
        # gets cropped; making the crop tool ignore the handles meant a fresh
        # page could only be cropped by drawing a rectangle freehand.
        if self.tool in (Tool.CROP, Tool.CROP_ALL) and kind == "move":
            kind = None
        self._fast_draw = True
        if kind in ("guide_h", "guide_v"):
            self._drag = (kind, data)
        elif kind == "move":
            self.interactionStarted.emit("Move crop")
            self._drag = ("move", (pos, self.crop_scene()))
        elif kind:
            self.interactionStarted.emit("Resize crop")
            self._drag = ("resize", (kind, self.crop_scene(), pos))
        elif self.tool in (Tool.CROP, Tool.CROP_ALL):
            self.interactionStarted.emit(
                "Crop every page" if self.tool == Tool.CROP_ALL else "Draw crop")
            self._drew_for_all = self.tool == Tool.CROP_ALL
            # remember what was there: a click without a drag must not commit a
            # one-pixel crop, which with the shared crop on would go to every
            # page in the project
            self._drag = ("new", (pos, list(self.edit.crop) if self.edit.crop
                                  else None))
            self.set_crop_scene(QRectF(pos, pos), commit=False)
        else:
            # CURSOR mode: clicking empty space pans instead of destroying the
            # crop that is already there
            self._drag = ("pan", [QPointF(ev.position()), QPointF(0.0, 0.0)])
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev):
        pos = self.mapToScene(ev.position().toPoint())
        sx, sy = self.scene_to_source(pos)
        self.cursorMoved.emit(sx, sy)

        if self._drag is None:
            if self.tool == Tool.BLUR:
                self.setCursor(Qt.CrossCursor)
                return
            if self.tool == Tool.CUT:
                self.setCursor(Qt.SplitHCursor)
                self._cut = self._cut_at(pos, ev.modifiers())
                self._announce_cut()
                self.viewport().update()
                return
            bi, bhandle = self._hit_blur(pos)
            if bi >= 0:
                if bhandle == "move":
                    self.setCursor(Qt.SizeAllCursor)
                else:
                    self.setCursor(
                        self._HANDLE_CURSORS[self._HANDLE_NAMES.index(bhandle)])
                return
            kind, data = self._hit(pos)
            if kind in self._HANDLE_NAMES:
                self.setCursor(self._HANDLE_CURSORS[self._HANDLE_NAMES.index(kind)])
            elif kind == "guide_h":
                self.setCursor(Qt.SplitVCursor)
            elif kind == "guide_v":
                self.setCursor(Qt.SplitHCursor)
            elif kind == "move":
                self.setCursor(Qt.SizeAllCursor)
            elif self.tool == Tool.CROP:
                self.setCursor(Qt.CrossCursor)
            else:
                self.setCursor(Qt.OpenHandCursor)
            return

        kind, data = self._drag
        if kind == "pan":
            # Scroll bars only move in whole pixels.  Truncating each delta and
            # then resetting the anchor anyway threw the remainder away on every
            # event - and at a high mouse polling rate most deltas are under a
            # pixel, so the view simply refused to move.  That is the hitching.
            # Carrying the fraction forward makes the drag smooth and exact.
            anchor, carry = data
            d = QPointF(ev.position()) - anchor + carry
            dx, dy = int(d.x()), int(d.y())
            if dx or dy:
                self.horizontalScrollBar().setValue(
                    self.horizontalScrollBar().value() - dx)
                self.verticalScrollBar().setValue(
                    self.verticalScrollBar().value() - dy)
            self._drag = ("pan", [QPointF(ev.position()),
                                  QPointF(d.x() - dx, d.y() - dy)])
            return
        if kind == "cut":
            self._cut = self._cut_at(pos, ev.modifiers())
            self._announce_cut()
            self.viewport().update()
            return
        if kind == "guide_h":
            self.guides_h = [pos.y() if abs(g - data) < 1e-9 else g for g in self.guides_h]
            self._drag = ("guide_h", pos.y())
            self.viewport().update()
            return
        if kind == "guide_v":
            self.guides_v = [pos.x() if abs(g - data) < 1e-9 else g for g in self.guides_v]
            self._drag = ("guide_v", pos.x())
            self.viewport().update()
            return
        if kind == "blur_new":
            i, start = data
            self.set_blur_scene(i, QRectF(start, pos))
            return
        if kind == "blur_move":
            i, _h, orig, start = data
            r = QRectF(orig)
            r.translate(pos - start)
            self.set_blur_scene(i, r)
            return
        if kind == "blur_resize":
            i, handle, orig, _start = data
            self.set_blur_scene(i, self._resized(handle, orig, pos, Qt.NoModifier))
            return
        if kind == "new":
            start, _previous = data
            self.set_crop_scene(QRectF(start, pos), commit=False)
            return
        if kind == "move":
            start, orig = data
            d = pos - start
            r = QRectF(orig)
            r.translate(d)
            self.set_crop_scene(r, commit=False)
            return
        if kind == "resize":
            handle, orig, start = data
            self.set_crop_scene(self._resized(handle, orig, pos, ev.modifiers()),
                                commit=False)
            return

    def mouseReleaseEvent(self, ev):
        if self._drag is None:
            return super().mouseReleaseEvent(ev)
        kind, self._drag_data = self._drag
        self._drag = None
        self._fast_draw = False
        self.unsetCursor()
        end_group = False

        if kind == "cut":
            if self._cut is not None:
                axis, frac = self._cut_fraction()
                self._cut = None
                self.tool = Tool.CURSOR
                self.toolFinished.emit()
                if 0.02 < frac < 0.98:
                    self.cutRequested.emit(axis, [frac])
                else:
                    self.statusMessage.emit(
                        "That cut would leave one side empty — place it "
                        "further in")
        elif kind == "new":
            r = self.crop_scene()
            px = 1.0 / (self.view_scale() or 1.0)
            if r.width() < 10 * px or r.height() < 10 * px:
                # a click, not a drag: put back whatever was there
                _start, previous = self._drag_data
                self.edit.crop = list(previous) if previous else None
                self.statusBar_hint = True
                self.statusMessage.emit(
                    "Drag out a rectangle to crop — a single click does nothing")
            else:
                # one crop per arming, so a stray drag cannot overwrite it
                to_all = getattr(self, "_drew_for_all", False)
                self.tool = Tool.CURSOR
                self.toolFinished.emit()
                self.cropDrawn.emit(bool(to_all))
            self._drew_for_all = False
            end_group = True
        elif kind == "blur_new":
            i = self.blur_selected
            # a click rather than a drag: give it a usable default size
            if 0 <= i < len(self.edit.blurs) and self.edit.blurs[i][2] < 12:
                s = self._scale or 1.0
                side = 260.0
                b = self.edit.blurs[i]
                b[0], b[1] = b[0] - side / 2, b[1] - side / 2
                b[2] = b[3] = side
                b[4] = side * 0.18
            self.tool = Tool.CURSOR
            self.toolFinished.emit()
            end_group = True
            self.blurSelected.emit(self.blur_selected)
        elif kind in ("move", "resize", "blur_move", "blur_resize"):
            end_group = True

        self.viewport().update()
        # Only report an edit when something was actually edited.  Panning,
        # clicking empty canvas, dragging a guide or a ruler all ended up here
        # too, and downstream that meant the current page's crop was copied
        # over every other page - without an undo step, because none of those
        # gestures opens one.  A click should not reshape the document.
        #
        # And it goes out *before* the undo group closes.  Handling this edit
        # propagates the crop to the other pages, and those changes have to
        # fall inside the group or a click that moves nothing - which discards
        # the group as a no-op - would push the propagation outside any undo
        # step at all.
        if kind in ("new", "move", "resize", "blur_new", "blur_move",
                    "blur_resize"):
            self.editChanged.emit()
        if end_group:
            self.interactionEnded.emit()

    # -------------------------------------------------------------- cutting
    def _cut_at(self, pos: QPointF, mods) -> tuple:
        """Where the cut line sits: vertical by default, horizontal with Shift."""
        crop = self.crop_scene()
        if mods & Qt.ShiftModifier:
            return ("h", min(max(pos.y(), crop.top()), crop.bottom()))
        return ("v", min(max(pos.x(), crop.left()), crop.right()))

    def _cut_fraction(self) -> tuple:
        if self._cut is None:
            return ("v", 0.5)
        axis, v = self._cut
        crop = self.crop_scene()
        if axis == "v":
            span = crop.width() or 1.0
            return ("v", (v - crop.left()) / span)
        span = crop.height() or 1.0
        return ("h", (v - crop.top()) / span)

    def _announce_cut(self) -> None:
        axis, frac = self._cut_fraction()
        crop = self.crop_scene()
        s = self._scale or 1.0
        total = (crop.width() if axis == "v" else crop.height()) / s
        a = total * max(0.0, min(1.0, frac))
        dpi = (self.edit.dpi if self.edit else 600.0) or 600.0
        self.statusMessage.emit(
            f"Cut {'across' if axis == 'h' else 'down'} at {frac * 100:.1f}%  —  "
            f"{a:.0f} px ({a / dpi:.2f}\") and {total - a:.0f} px "
            f"({(total - a) / dpi:.2f}\")   ·  hold Shift to cut the other way")

    def cut_even(self, parts: int = 2, axis: str = "v") -> None:
        """A measured cut: equal halves, thirds, quarters."""
        if self.edit is None or parts < 2:
            return
        self.cutRequested.emit(axis, [i / float(parts) for i in range(1, parts)])

    def _resized(self, handle: str, orig: QRectF, pos: QPointF, mods) -> QRectF:
        r = QRectF(orig)
        if "l" in handle:
            r.setLeft(pos.x())
        if "r" in handle:
            r.setRight(pos.x())
        if "t" in handle:
            r.setTop(pos.y())
        if "b" in handle:
            r.setBottom(pos.y())
        r = r.normalized()
        ratio = self.lock_aspect
        if (mods & Qt.ShiftModifier) and not ratio:
            ratio = orig.width() / orig.height() if orig.height() else None
        if ratio:
            if handle in ("l", "r"):
                r.setHeight(r.width() / ratio)
            elif handle in ("t", "b"):
                r.setWidth(r.height() * ratio)
            else:
                if r.width() / max(r.height(), 1e-6) > ratio:
                    r.setWidth(r.height() * ratio)
                else:
                    r.setHeight(r.width() / ratio)
        if r.width() < 4:
            r.setWidth(4)
        if r.height() < 4:
            r.setHeight(4)
        return r

    # --------------------------------------------------------------- edit ops
    def apply_angle_delta(self, delta: float) -> None:
        if self.edit is None or abs(delta) < 1e-9:
            return
        old = self.edit.angle
        new = round(max(-45.0, min(45.0, old + delta)), 3)
        self.set_angle(new)

    def set_angle(self, new: float) -> None:
        if self.edit is None:
            return
        old = self.edit.angle
        if abs(new - old) < 1e-9:
            return
        remap_crop_for_angle(self.edit, old, new)
        self.edit.angle = new
        self._update_scene_rect()
        self.viewport().update()
        self.editChanged.emit()

    def nudge_crop(self, dx: float, dy: float, resize: bool = False) -> None:
        if self.edit is None:
            return
        r = self.crop_scene()
        s = self._scale or 1.0
        if resize:
            r.setWidth(max(4.0, r.width() + dx * s))
            r.setHeight(max(4.0, r.height() + dy * s))
        else:
            r.translate(dx * s, dy * s)
        self.set_crop_scene(r)

    def reset_crop(self) -> None:
        if self.edit is None:
            return
        self.edit.crop = None
        self.editChanged.emit()
        self.viewport().update()

    def select_all(self) -> None:
        if self.edit is None:
            return
        W, H = self.page_canvas()
        self.set_crop_scene(QRectF(0, 0, W, H))

    def add_guide(self, horizontal: bool = True) -> None:
        """Drop a guide across the middle of the *view*, not the page.

        Zoomed in on the top of a scan, a guide placed at the centre of the
        page lands somewhere off-screen and has to be hunted for.  Successive
        guides step apart by a fixed number of screen pixels so the second one
        is not hidden exactly under the first.
        """
        c = self.mapToScene(self.viewport().rect().center())
        step = 30.0 / (self.view_scale() or 1.0)
        if horizontal:
            self.guides_h.append(c.y() + len(self.guides_h) * step)
        else:
            self.guides_v.append(c.x() + len(self.guides_v) * step)
        self.viewport().update()

    def clear_guides(self) -> None:
        self.guides_h.clear()
        self.guides_v.clear()
        self.viewport().update()

    # ------------------------------------------------------- files from disk
    # A QGraphicsView switches drops on for its viewport by itself, so it can
    # hand them to items in the scene.  There are no such items here, so a
    # folder dropped on the page was accepted and then quietly discarded - and
    # the middle of the window is exactly where you aim when you drag a folder
    # in.  Catching them here and passing them up is what makes the whole
    # window a drop target rather than just the page grid.
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            return
        super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            return
        super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        if not ev.mimeData().hasUrls():
            super().dropEvent(ev)
            return
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        ev.acceptProposedAction()
        if paths:
            self.filesDropped.emit(paths)

    # -------------------------------------------------------------- keyboard
    def keyPressEvent(self, ev):
        # Space used to be hold-to-pan.  It is a guide key now, and panning has
        # two ways left that do not need a modifier at all: middle-drag, and
        # the wheel, which scrolls rather than zooms.
        step = 10.0 if ev.modifiers() & Qt.ShiftModifier else 1.0
        k = ev.key()
        resize = bool(ev.modifiers() & Qt.AltModifier)
        big = bool(ev.modifiers() & Qt.ShiftModifier)

        if k in (Qt.Key_Delete, Qt.Key_Backspace) and self.blur_selected >= 0:
            self.delete_blur()
            return
        if self.blur_selected >= 0 and k in (Qt.Key_Left, Qt.Key_Right,
                                             Qt.Key_Up, Qt.Key_Down):
            self.interactionStarted.emit("Move blur")
            s = self._scale or 1.0
            r = self.blur_scene(self.blur_selected)
            r.translate(step * s * ((k == Qt.Key_Right) - (k == Qt.Key_Left)),
                        step * s * ((k == Qt.Key_Down) - (k == Qt.Key_Up)))
            self.set_blur_scene(self.blur_selected, r)
            self.editChanged.emit()
            return

        if k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            self.interactionStarted.emit("Resize crop" if resize else "Nudge crop")
            self.nudge_crop(-step if k == Qt.Key_Left else step if k == Qt.Key_Right else 0,
                            -step if k == Qt.Key_Up else step if k == Qt.Key_Down else 0,
                            resize)
        elif k == Qt.Key_BracketLeft:
            self.interactionStarted.emit("Rotate")
            self.apply_angle_delta(-0.1 if big else -0.01)
        elif k == Qt.Key_BracketRight:
            self.interactionStarted.emit("Rotate")
            self.apply_angle_delta(0.1 if big else 0.01)
        else:
            super().keyPressEvent(ev)

