"""Vector icons drawn at runtime.

Everything is painted from code on a 24x24 grid, so the app ships as pure
Python with no image assets to lose, and icons stay crisp on a high-DPI
display because they are re-rendered at the device pixel ratio.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF)

INK = "#d6dae0"
ACCENT = "#ffc247"

_cache: dict = {}


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------

def _stroke(p: QPainter, color: QColor, width: float = 2.0) -> None:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)


def _poly(*pts) -> QPolygonF:
    return QPolygonF([QPointF(x, y) for x, y in pts])


# --------------------------------------------------------------------------
# the icons
# --------------------------------------------------------------------------

def _open(p, c):
    _stroke(p, c)
    path = QPainterPath()
    path.moveTo(3, 19)
    path.lineTo(3, 6)
    path.lineTo(9.5, 6)
    path.lineTo(11.5, 8.5)
    path.lineTo(18, 8.5)
    path.lineTo(18, 10.5)
    p.drawPath(path)
    p.drawPolygon(_poly((3, 19), (6.4, 11.5), (21, 11.5), (17.6, 19)))


def _save(p, c):
    _stroke(p, c)
    path = QPainterPath()
    path.moveTo(4.5, 4.5)
    path.lineTo(16.5, 4.5)
    path.lineTo(19.5, 7.5)
    path.lineTo(19.5, 19.5)
    path.lineTo(4.5, 19.5)
    path.closeSubpath()
    p.drawPath(path)
    p.drawRect(QRectF(8, 4.5, 8, 5))
    p.drawRect(QRectF(7.5, 13, 9, 6.5))


def _save_as(p, c):
    _stroke(p, c, 1.9)
    path = QPainterPath()
    path.moveTo(4, 4.5)
    path.lineTo(14.5, 4.5)
    path.lineTo(17, 7)
    path.lineTo(17, 12)
    p.drawPath(path)
    path2 = QPainterPath()
    path2.moveTo(17, 17.5)
    path2.lineTo(17, 19.5)
    path2.lineTo(4, 19.5)
    path2.lineTo(4, 4.5)
    p.drawPath(path2)
    p.drawRect(QRectF(7, 4.5, 6.5, 4))
    # pencil, kept inside the box so its tip is not shaved off
    _stroke(p, QColor(ACCENT), 1.9)
    p.drawPolygon(_poly((13.2, 20.6), (13.2, 18.0), (18.9, 12.3), (21.5, 14.9),
                        (15.8, 20.6)))
    p.drawLine(QPointF(17.2, 14.2), QPointF(19.8, 16.8))


def _export(p, c):
    _stroke(p, c)
    path = QPainterPath()
    path.moveTo(4, 14)
    path.lineTo(4, 20)
    path.lineTo(20, 20)
    path.lineTo(20, 14)
    p.drawPath(path)
    p.drawLine(QPointF(12, 3.5), QPointF(12, 15))
    p.drawPolyline(_poly((7.5, 8), (12, 3.5), (16.5, 8)))


def _undo(p, c):
    _stroke(p, c)
    path = QPainterPath()
    path.moveTo(4.5, 10.5)
    path.cubicTo(9, 4.5, 17.5, 5.2, 19.5, 11.5)
    path.cubicTo(20.8, 15.6, 18.6, 19.4, 14.5, 20.2)
    p.drawPath(path)
    p.drawPolyline(_poly((9.8, 10.8), (4.3, 10.6), (4.9, 5.2)))


def _redo(p, c):
    p.save()
    p.translate(24, 0)
    p.scale(-1, 1)
    _undo(p, c)
    p.restore()


def _grid(p, c):
    _stroke(p, c, 1.7)
    p.drawRect(QRectF(4, 4, 16, 16))
    for i in (1, 2):
        v = 4 + 16 * i / 3.0
        p.drawLine(QPointF(v, 4), QPointF(v, 20))
        p.drawLine(QPointF(4, v), QPointF(20, v))


def _cursor(p, c):
    _stroke(p, c, 1.8)
    p.setBrush(c)
    p.drawPolygon(_poly((6, 3.5), (6, 18.5), (10, 14.8), (12.6, 20.5),
                        (15.2, 19.2), (12.6, 13.8), (17.6, 13.4)))


def _crop(p, c):
    _stroke(p, c)
    p.drawLine(QPointF(7.5, 2.5), QPointF(7.5, 16.5))
    p.drawLine(QPointF(7.5, 16.5), QPointF(21.5, 16.5))
    p.drawLine(QPointF(2.5, 7.5), QPointF(16.5, 7.5))
    p.drawLine(QPointF(16.5, 7.5), QPointF(16.5, 21.5))


def _level(p, c):
    _stroke(p, c, 1.7)
    p.drawLine(QPointF(3, 17.5), QPointF(21, 17.5))
    _stroke(p, QColor(ACCENT), 2.1)
    p.drawLine(QPointF(4.2, 13.2), QPointF(19.6, 7.4))
    p.setBrush(QColor(ACCENT))
    p.drawEllipse(QPointF(4.2, 13.2), 1.9, 1.9)
    p.drawEllipse(QPointF(19.6, 7.4), 1.9, 1.9)


def _deskew(p, c):
    _stroke(p, c, 1.8)
    p.drawLine(QPointF(3.5, 19), QPointF(20.5, 19))
    p.drawLine(QPointF(3.5, 19), QPointF(18, 6.5))
    path = QPainterPath()
    path.moveTo(13.2, 19)
    path.arcTo(QRectF(3.5 - 9.7, 19 - 9.7, 19.4, 19.4), 0, 40)
    _stroke(p, QColor(ACCENT), 1.8)
    p.drawPath(path)


def _fitpage(p, c):
    _stroke(p, c, 1.7)
    pen = p.pen()
    pen.setStyle(Qt.DashLine)
    p.setPen(pen)
    p.drawRect(QRectF(2.5, 2.5, 19, 19))
    _stroke(p, QColor(ACCENT), 2.0)
    p.drawRect(QRectF(6.5, 6.5, 11, 11))


def _auto(p, c):
    _stroke(p, c, 2.0)
    p.drawLine(QPointF(4, 20), QPointF(15.5, 8.5))
    p.drawLine(QPointF(13.5, 6.5), QPointF(17.5, 10.5))
    _stroke(p, QColor(ACCENT), 1.6)
    for cx, cy, r in ((18.3, 4.8, 2.1), (20.0, 11.4, 1.5), (12.2, 3.2, 1.5)):
        p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))


def _copyframe(p, c):
    _stroke(p, c, 1.8)
    p.drawRect(QRectF(3, 3, 11, 13))
    _stroke(p, QColor(ACCENT), 1.8)
    p.drawRect(QRectF(10, 8, 11, 13))


def _spread(p, c):
    _stroke(p, c, 1.8)
    p.drawRect(QRectF(2.5, 4.5, 8.5, 15))
    p.drawRect(QRectF(13, 4.5, 8.5, 15))
    _stroke(p, QColor(ACCENT), 1.6)
    p.drawLine(QPointF(12, 2.5), QPointF(12, 21.5))


def _fitview(p, c):
    _stroke(p, c, 1.9)
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        ox = 12 - sx * 8.5
        oy = 12 - sy * 8.5
        p.drawPolyline(_poly((ox + sx * 5, oy), (ox, oy), (ox, oy + sy * 5)))
    p.drawRect(QRectF(8.5, 8.5, 7, 7))


def _one2one(p, c):
    _stroke(p, c, 1.8)
    p.drawRect(QRectF(2.5, 5.5, 19, 13))
    f = p.font()
    f.setPixelSize(9)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QPen(c))
    p.drawText(QRectF(2.5, 5.5, 19, 13), Qt.AlignCenter, "1:1")


def _done(p, c):
    _stroke(p, QColor("#6fd07f"), 2.6)
    p.drawPolyline(_poly((4, 12.5), (9.5, 18), (20, 5.5)))


def _trash(p, c):
    _stroke(p, c, 1.8)
    p.drawLine(QPointF(3.5, 6.5), QPointF(20.5, 6.5))
    p.drawPolyline(_poly((9, 6.5), (9, 3.5), (15, 3.5), (15, 6.5)))
    path = QPainterPath()
    path.moveTo(5.5, 6.5)
    path.lineTo(6.6, 20.5)
    path.lineTo(17.4, 20.5)
    path.lineTo(18.5, 6.5)
    p.drawPath(path)
    p.drawLine(QPointF(10, 10), QPointF(10, 17))
    p.drawLine(QPointF(14, 10), QPointF(14, 17))


def _up(p, c):
    _stroke(p, c, 2.2)
    p.drawLine(QPointF(12, 20), QPointF(12, 5))
    p.drawPolyline(_poly((5.5, 11.5), (12, 5), (18.5, 11.5)))


def _down(p, c):
    p.save()
    p.translate(0, 24)
    p.scale(1, -1)
    _up(p, c)
    p.restore()


def _sort(p, c):
    _stroke(p, c, 2.0)
    for i, w in enumerate((14, 10, 6)):
        y = 6.5 + i * 5.5
        p.drawLine(QPointF(3, y), QPointF(3 + w, y))
    _stroke(p, QColor(ACCENT), 2.0)
    p.drawLine(QPointF(18.8, 4.5), QPointF(18.8, 19.5))
    p.drawPolyline(_poly((16.2, 16.6), (18.8, 19.5), (21.4, 16.6)))


def _rot_cw(p, c):
    _stroke(p, c, 2.0)
    path = QPainterPath()
    path.arcMoveTo(QRectF(4, 4, 16, 16), 250)
    path.arcTo(QRectF(4, 4, 16, 16), 250, -290)
    p.drawPath(path)
    p.drawPolyline(_poly((11.5, 2.2), (15.4, 5.4), (11.9, 8.6)))


def _rot_ccw(p, c):
    p.save()
    p.translate(24, 0)
    p.scale(-1, 1)
    _rot_cw(p, c)
    p.restore()


def _blur(p, c):
    """A rectangle whose right half dissolves into dots."""
    _stroke(p, c, 1.8)
    p.drawRect(QRectF(3, 5, 18, 14))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    for row, ys in enumerate((8.5, 12.0, 15.5)):
        for col, xs in enumerate((7.0, 10.5, 14.0, 17.5)):
            r = 1.55 - col * 0.32
            p.setOpacity(1.0 - col * 0.2)
            p.drawEllipse(QPointF(xs, ys), r, r)
    p.setOpacity(1.0)


def _goto(p, c):
    """A page dropping into a numbered slot."""
    _stroke(p, c, 1.8)
    p.drawRect(QRectF(3, 4, 8, 16))
    for i in range(3):
        y = 5.5 + i * 5.5
        p.drawLine(QPointF(14.5, y), QPointF(21, y))
    _stroke(p, QColor(ACCENT), 2.0)
    p.drawLine(QPointF(7, 7), QPointF(7, 16))
    p.drawPolyline(_poly((4.4, 13.4), (7, 16), (9.6, 13.4)))


def _link(p, c):
    _stroke(p, c, 1.9)
    p.drawRect(QRectF(2.5, 5.5, 8, 13))
    p.drawRect(QRectF(13.5, 5.5, 8, 13))
    _stroke(p, QColor(ACCENT), 2.0)
    p.drawLine(QPointF(10.5, 12), QPointF(13.5, 12))


def _cut(p, c):
    """Scissors on a dashed line.

    An earlier attempt drew two panels with a line between them, which in a
    tool bar that already has "spread", "join" and "link" was a fourth
    two-rectangle icon and told you nothing.  Scissors are unmistakable.
    """
    _stroke(p, c, 1.7)
    pen = p.pen()
    pen.setStyle(Qt.DashLine)
    p.setPen(pen)
    p.drawLine(QPointF(2.5, 12), QPointF(21.5, 12))     # the line being cut

    _stroke(p, QColor(ACCENT), 1.9)
    p.drawLine(QPointF(8.0, 4.0), QPointF(16.5, 15.5))  # blades, crossing
    p.drawLine(QPointF(16.5, 4.0), QPointF(8.0, 15.5))
    p.setBrush(Qt.NoBrush)
    _stroke(p, QColor(ACCENT), 1.7)
    p.drawEllipse(QPointF(7.2, 17.6), 2.5, 2.5)         # finger rings
    p.drawEllipse(QPointF(17.3, 17.6), 2.5, 2.5)


def _join(p, c):
    """This sheet plus that sheet.

    Two arrowheads pointing at each other merged into a solid diamond at
    22 px, and a thin divider is what "spread" already looks like.  A plus
    sign survives the size and cannot be mistaken for either.
    """
    _stroke(p, c, 1.8)
    p.drawRect(QRectF(1.8, 5.5, 6.6, 14.0))
    p.drawRect(QRectF(15.6, 5.5, 6.6, 14.0))
    _stroke(p, QColor(ACCENT), 2.2)
    p.drawLine(QPointF(9.4, 12.5), QPointF(14.6, 12.5))
    p.drawLine(QPointF(12.0, 9.9), QPointF(12.0, 15.1))


def _matchsize(p, c):
    """Two frames of different heights and an equals sign: make these agree."""
    _stroke(p, c, 1.7)
    p.drawRect(QRectF(1.8, 4.0, 7.0, 16.0))
    _stroke(p, QColor(ACCENT), 1.9)
    p.drawRect(QRectF(15.2, 6.0, 7.0, 12.0))
    _stroke(p, c, 1.6)
    p.drawLine(QPointF(10.4, 10.6), QPointF(13.6, 10.6))
    p.drawLine(QPointF(10.4, 13.4), QPointF(13.6, 13.4))


def _book(p, c):
    """An open book: two leaves meeting at a spine."""
    _stroke(p, c, 1.8)
    p.drawLine(QPointF(12, 6), QPointF(12, 20))
    p.drawLine(QPointF(12, 6.5), QPointF(3.5, 4.5))
    p.drawLine(QPointF(3.5, 4.5), QPointF(3.5, 18))
    p.drawLine(QPointF(3.5, 18), QPointF(12, 20))
    _stroke(p, QColor(ACCENT), 1.8)
    p.drawLine(QPointF(12, 6.5), QPointF(20.5, 4.5))
    p.drawLine(QPointF(20.5, 4.5), QPointF(20.5, 18))
    p.drawLine(QPointF(20.5, 18), QPointF(12, 20))


_DRAW = {
    "book": _book,
    "cut": _cut, "join": _join, "matchsize": _matchsize,
    "open": _open, "save": _save, "save_as": _save_as, "export": _export,
    "undo": _undo, "redo": _redo, "grid": _grid, "cursor": _cursor,
    "crop": _crop, "level": _level, "deskew": _deskew, "fitpage": _fitpage,
    "auto": _auto, "copyframe": _copyframe, "spread": _spread,
    "fitview": _fitview, "one2one": _one2one, "done": _done, "trash": _trash,
    "up": _up, "down": _down, "sort": _sort, "rot_cw": _rot_cw,
    "rot_ccw": _rot_ccw, "link": _link, "goto": _goto, "blur": _blur,
}


# The drawings all aim to sit inside this box on the 24-unit grid.  Anything
# that strays outside gets scaled down to fit rather than being silently shaved
# off at the pixmap edge, which is what "cut off" icons look like.
SAFE = 1.4
_bounds: dict = {}


def _measure(name: str) -> tuple:
    """Ink bounds of an icon, in 24-grid units, measured once and cached."""
    if name in _bounds:
        return _bounds[name]
    import numpy as np
    from PySide6.QtGui import QImage

    S = 96
    img = QImage(S, S, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(S / 24.0, S / 24.0)
    _DRAW[name](p, QColor(INK))
    p.end()
    buf = np.frombuffer(img.constBits(), np.uint8).reshape(
        S, img.bytesPerLine() // 4, 4)
    alpha = buf[:, :S, 3].copy()          # copy: the view borrows QImage memory
    ys, xs = np.nonzero(alpha > 12)
    if xs.size == 0:
        box = (0.0, 0.0, 24.0, 24.0)
    else:
        k = 24.0 / S
        box = (float(xs.min() * k), float(ys.min() * k),
               float((xs.max() + 1) * k), float((ys.max() + 1) * k))
    _bounds[name] = box
    return box


def _fit_transform(name: str):
    """Scale and offset that pull a stray drawing back inside the box.

    Returns None when the drawing already fits.  Note that an icon can need
    only a nudge and no shrink at all - returning a scale of 1.0 in that case
    still means the translation has to be applied.
    """
    l, t, r, b = _measure(name)
    lo, hi = SAFE, 24.0 - SAFE
    if l >= lo - 0.01 and t >= lo - 0.01 and r <= hi + 0.01 and b <= hi + 0.01:
        return None
    w, h = max(r - l, 1e-6), max(b - t, 1e-6)
    scale = min((hi - lo) / w, (hi - lo) / h, 1.0)
    cx, cy = (l + r) / 2.0, (t + b) / 2.0
    return scale, 12.0 - cx * scale, 12.0 - cy * scale


def pixmap(name: str, size: int = 22, color: str = INK, ratio: float = 1.0) -> QPixmap:
    px = QPixmap(int(size * ratio), int(size * ratio))
    px.setDevicePixelRatio(ratio)
    px.fill(Qt.transparent)
    draw = _DRAW.get(name)
    if draw is None:
        return px
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    # A painter on a pixmap with devicePixelRatio 2 already maps logical units
    # to twice as many device pixels, so the scale here is in LOGICAL units.
    # Multiplying by the ratio as well doubles it, and only the top-left
    # quarter of the drawing lands inside the pixmap.
    p.scale(size / 24.0, size / 24.0)
    fit = _fit_transform(name)
    if fit is not None:
        scale, dx, dy = fit
        p.translate(dx, dy)
        p.scale(scale, scale)
    draw(p, QColor(color))
    p.end()
    return px


def icon(name: str, size: int = 22, color: str = INK) -> QIcon:
    key = (name, size, color)
    if key not in _cache:
        ic = QIcon()
        for ratio in (1.0, 2.0):
            ic.addPixmap(pixmap(name, size, color, ratio))
        _cache[key] = ic
    return _cache[key]


# --------------------------------------------------------------------------
# application icon
# --------------------------------------------------------------------------
# Lives here rather than in the build script so the running window, the task
# bar and the .exe all show the same drawing.

APP_BG_TOP = "#3b4149"
APP_BG_BOTTOM = "#22252a"
APP_PAPER = "#f2f0ea"
APP_INK = "#5c626c"


def draw_app_icon(p: QPainter, detail: bool = True) -> None:
    """The app mark on a 64-unit grid: a tilted sheet inside crop marks."""
    g = QLinearGradient(0, 0, 0, 64)
    g.setColorAt(0.0, QColor(APP_BG_TOP))
    g.setColorAt(1.0, QColor(APP_BG_BOTTOM))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawRoundedRect(QRectF(1.5, 1.5, 61, 61), 13, 13)

    p.save()
    p.translate(32, 33)
    p.rotate(-7)                       # the way a sheet lands on the glass
    p.translate(-32, -33)
    p.setBrush(QColor(0, 0, 0, 70))
    p.drawRoundedRect(QRectF(17, 12, 30, 42), 2, 2)
    p.setBrush(QColor(APP_PAPER))
    p.drawRoundedRect(QRectF(16, 11, 30, 42), 2, 2)
    if detail:
        pen = QPen(QColor(APP_INK), 2.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for i, w in enumerate((20, 20, 15, 20, 11)):
            y = 20 + i * 6.6
            p.drawLine(QPointF(21, y), QPointF(21 + w, y))
    p.restore()

    pen = QPen(QColor(ACCENT), 3.4 if detail else 4.2)
    pen.setCapStyle(Qt.SquareCap)
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    arm = 9.0 if detail else 10.0
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        x, y = 32 - sx * 20.0, 33 - sy * 22.0
        p.drawPolyline(QPolygonF([QPointF(x + sx * arm, y), QPointF(x, y),
                                  QPointF(x, y + sy * arm)]))


def app_pixmap(size: int = 64) -> QPixmap:
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 64.0, size / 64.0)
    draw_app_icon(p, detail=size >= 32)
    p.end()
    return px


def app_icon() -> QIcon:
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(app_pixmap(s))
    return ic
