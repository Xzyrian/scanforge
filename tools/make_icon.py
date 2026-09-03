"""Generate the application icon.

Drawn in code like everything else in the interface, then written out as a
multi-resolution .ico for the executable and a .png for anywhere else.  A
tilted sheet with crop marks: the two things the app actually does.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication

from scanforge.ui import icons

SIZES = (256, 128, 64, 48, 32, 24, 16)
BG_TOP = "#3b4149"
BG_BOTTOM = "#22252a"
PAPER = "#f2f0ea"
INK = "#5c626c"
ACCENT = "#ffc247"


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 64.0, size / 64.0)
    icons.draw_app_icon(p, detail=size >= 32)
    p.end()
    return img


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "tools")
    os.makedirs(out_dir, exist_ok=True)

    png = os.path.join(out_dir, "scanforge.png")
    render(256).save(png)

    # Pillow writes a proper multi-resolution .ico; Qt cannot
    from PIL import Image
    frames = []
    for s in sorted(SIZES):
        tmp = os.path.join(out_dir, f"_icon_{s}.png")
        render(s).save(tmp)
        frames.append(Image.open(tmp).convert("RGBA"))
    ico = os.path.join(out_dir, "scanforge.ico")
    frames[-1].save(ico, format="ICO",
                    sizes=[(f.width, f.height) for f in frames])
    for s in SIZES:
        tmp = os.path.join(out_dir, f"_icon_{s}.png")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    print(f"wrote {ico}  ({os.path.getsize(ico) / 1024:.0f} KB, "
          f"{len(frames)} sizes)")
    print(f"wrote {png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
