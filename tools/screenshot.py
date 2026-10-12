"""Render the README screenshot offscreen.

Builds a small synthetic project - the same fake scans the test suite uses -
loads it into a real MainWindow, and paints the window to docs/screenshot.png.
Doing it this way means the picture in the README is never stale: regenerate it
after a layout change instead of hunting for a good moment to hit PrtSc.

    python tools/screenshot.py [--width 1600] [--height 1000] [--out PATH]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtGui import QImage                                 # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget              # noqa: E402

from scanforge.ui.main_window import MainWindow, dark_palette    # noqa: E402


def build_scans(folder: str, n: int) -> list[str]:
    from test_pipeline import make_scan
    os.makedirs(folder, exist_ok=True)
    out = []
    for i in range(n):
        p = os.path.join(folder, f"Sony_KV-16G1_{i + 1:04d}.tif")
        make_scan(w=2400, h=3400, skew=0.55 - 0.18 * i, margin=95, path=p)
        out.append(p)
    return out


def settle(app: QApplication, seconds: float = 8.0, done=lambda: False) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        if done():
            break
        time.sleep(0.02)
    app.processEvents()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--pages", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "screenshot.png"))
    args = ap.parse_args()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    from scanforge.ui import theme
    theme.apply(app)

    tmp = tempfile.mkdtemp(prefix="sf_shot_")
    scans = build_scans(os.path.join(tmp, "Sony_KV-16G1"), args.pages)

    win = MainWindow()
    win.resize(args.width, args.height)
    win.show()
    app.processEvents()

    win.add_paths([os.path.dirname(scans[0])])     # what a folder drop calls
    settle(app, 60.0, lambda: not win.queue._icon_timer.isActive())

    # a page part-way through the document, so the picture shows the app in
    # use rather than sitting at a cold start
    if len(win.project.pages) > 2:
        win.queue.select(win.project.pages[2].uid)
        settle(app, 20.0, lambda: win.canvas._pix is not None)
    win.fit_to_page()
    settle(app, 3.0)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    img = QImage(win.size(), QImage.Format_ARGB32)
    img.fill(0)
    QWidget.render(win, img)
    img.convertToFormat(QImage.Format_RGB32).save(args.out, "PNG")

    win._dirty = False
    win.close()
    app.processEvents()
    print(f"wrote {args.out}  ({img.width()}x{img.height()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
