"""Render every icon large, and flag any whose ink touches the edge of its box.

An icon drawn past the 24x24 grid gets silently clipped by the pixmap, which
reads as "cut off" in the toolbar.  Rendering at 96px and looking at which
pixels land in the outermost ring catches it mechanically.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from scanforge.ui import icons

CELL = 96
PAD = 26


def alpha_of(name: str, size: int = CELL, ratio: float = 1.0) -> np.ndarray:
    """Alpha channel of what the app actually puts on a button.

    Goes through icons.pixmap() rather than the raw draw function, so the
    fit-to-box safety net is measured too, and takes a device pixel ratio,
    because the high-DPI variant is a separate rendering path that can be
    broken while the 1x one looks perfect.
    """
    px = icons.pixmap(name, size, ratio=ratio)
    img = px.toImage().convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    buf = np.frombuffer(img.constBits(), np.uint8).reshape(
        h, img.bytesPerLine() // 4, 4)
    # copy: the numpy view borrows the QImage's buffer, which is freed when img
    # falls out of scope, and reading it afterwards returns garbage
    return buf[:, :w, 3].copy()


def report():
    app = QApplication.instance() or QApplication(sys.argv)
    names = list(icons._DRAW)
    bad = []

    print(f"{'icon':<12} {'left':>5} {'top':>5} {'right':>6} {'bottom':>7}   "
          f"edge ink at 1x / 1.5x / 2x")
    print("-" * 74)
    for name in names:
        a = alpha_of(name)
        rings = []
        for r in (1.0, 1.5, 2.0):
            b = alpha_of(name, 22, r)
            rings.append(int(max(b[0].max(), b[-1].max(),
                                 b[:, 0].max(), b[:, -1].max())))
        ys, xs = np.nonzero(a > 12)
        if xs.size == 0:
            print(f"{name:<12}  EMPTY")
            bad.append(name)
            continue
        # convert back to 24-unit space
        s = 24.0 / CELL
        l, t = xs.min() * s, ys.min() * s
        r, b = (xs.max() + 1) * s, (ys.max() + 1) * s
        flag = "CLIPPED" if max(rings) > 12 else ""
        if flag:
            bad.append(name)
        print(f"{name:<12} {l:5.1f} {t:5.1f} {r:6.1f} {b:7.1f}   "
              f"{rings[0]:>3} {rings[1]:>4} {rings[2]:>4}  {flag}")

    print()
    if bad:
        print(f"{len(bad)} icons touch the edge: " + ", ".join(bad))
    else:
        print("no icon touches the edge of its box")
    return names, bad


def sheet(names, path):
    cols = 7
    rows = (len(names) + cols - 1) // cols
    W = cols * (CELL + PAD)
    H = rows * (CELL + PAD + 18)
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(QColor(47, 50, 56))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    f = p.font()
    f.setPixelSize(12)
    p.setFont(f)
    for i, name in enumerate(names):
        cx = (i % cols) * (CELL + PAD) + PAD / 2
        cy = (i // cols) * (CELL + PAD + 18) + PAD / 2
        p.setPen(QColor(90, 95, 104))
        p.drawRect(QRectF(cx, cy, CELL, CELL))
        p.drawPixmap(int(cx), int(cy), icons.pixmap(name, CELL))
        p.setPen(QColor(200, 205, 212))
        p.drawText(QRectF(cx - PAD / 2, cy + CELL + 2, CELL + PAD, 16),
                   Qt.AlignCenter, name)
    p.end()
    img.save(path)
    print(f"\ncontact sheet -> {path}")


if __name__ == "__main__":
    names, bad = report()
    sheet(names, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "icon_sheet.png"))
    sys.exit(0)
