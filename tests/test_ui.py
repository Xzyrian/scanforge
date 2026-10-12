"""Headless UI smoke test.

Runs the real widgets under Qt's offscreen platform: builds the window, loads
pages, drives every action a user would reach for, and forces an actual paint
of the canvas and the spread view so painter code is executed rather than just
imported.

Page loading is asynchronous now, so anything that needs pixels waits on the
event loop rather than assuming the decode already happened.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import (QAbstractAnimation, QPoint, QPointF, QRectF, Qt,
                            QEvent)
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget

from scanforge import proxycache
from scanforge.export import (ExportSettings, format_name, output_path,
                              pdf_path, resolve_pdf_dpi)
from scanforge.ui.canvas import Tool
from scanforge.ui.main_window import MainWindow, dark_palette
from scanforge.ui.spread_view import SpreadPane
from scanforge.ui.export_dialog import ExportDialog
from scanforge.ui import icons

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_pipeline import make_scan

TMP = tempfile.mkdtemp(prefix="scanforge_ui_")
FAILURES = []
APP = None


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def pump(seconds=0.05):
    t0 = time.time()
    while time.time() - t0 < seconds:
        APP.processEvents()
        time.sleep(0.005)


def wait_until(cond, timeout=60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        APP.processEvents()
        time.sleep(0.01)
    return cond()


def paint(widget) -> QImage:
    img = QImage(widget.size(), QImage.Format_ARGB32)
    img.fill(0)
    QWidget.render(widget, img)      # QGraphicsView overloads render()
    return img


def nonblank(img: QImage) -> bool:
    arr = np.frombuffer(img.constBits(), np.uint8).reshape(
        img.height(), img.bytesPerLine() // 4, 4)
    return int(arr[..., :3].std()) > 3


def make_pages(folder, n=4):
    os.makedirs(folder, exist_ok=True)
    paths = []
    for i in range(n):
        p = os.path.join(folder, f"page_{i:02d}.tif")
        make_scan(w=2400, h=3400, skew=0.4 - 0.25 * i, margin=90, path=p)
        paths.append(p)
    return paths


def main():
    global APP
    print("=" * 72)
    print("ScanForge UI smoke test  (offscreen)")
    print("=" * 72)

    APP = QApplication.instance() or QApplication(sys.argv)
    APP.setStyle("Fusion")
    APP.setPalette(dark_palette())

    scans = os.path.join(TMP, "scans")
    paths = make_pages(scans)
    print(f"\nfixture: {len(paths)} pages in {scans}")

    # ------------------------------------------------------------- icons
    print("\nicons")
    bad = [n for n in icons._DRAW if icons.pixmap(n, 22).isNull()]
    check("every icon renders", not bad, str(bad))

    from icon_sheet import alpha_of
    # Check every device pixel ratio: the high-DPI variant is a separate
    # rendering path, and it was once drawing at double scale so only the
    # top-left quarter of each icon survived, while 1x looked perfect.
    for ratio in (1.0, 1.5, 2.0):
        clipped = []
        for name in icons._DRAW:
            a = alpha_of(name, 22, ratio)
            if max(a[0].max(), a[-1].max(), a[:, 0].max(), a[:, -1].max()) > 12:
                clipped.append(name)
        check(f"no icon is clipped at {ratio:g}x", not clipped, str(clipped))

    empty = [n for n in icons._DRAW if alpha_of(n, 96).max() < 12]
    check("no icon draws nothing", not empty, str(empty))

    # the drawing must occupy a consistent share of the button at every ratio
    spans = []
    for ratio in (1.0, 2.0):
        a = alpha_of("grid", 22, ratio)
        ys, xs = np.nonzero(a > 12)
        spans.append((xs.max() - xs.min() + 1) / a.shape[1])
    check("icons keep their proportions across ratios",
          abs(spans[0] - spans[1]) < 0.10,
          f"{spans[0]:.0%} at 1x vs {spans[1]:.0%} at 2x")

    win = MainWindow()
    win.resize(1500, 900)
    win.show()
    pump()
    check("window constructed", win.isVisible())
    # a settings file of its own, so a test run never edits the real recent
    # list, window position or anything else a person has
    from PySide6.QtCore import QSettings
    win.settings = QSettings(os.path.join(TMP, "settings.ini"), QSettings.IniFormat)

    # ---- the first screen
    # It used to be one line of grey text framed by a disabled tool column, a
    # disabled panel and an empty grid.  Now it says what the app is for and
    # offers the ways in, and nothing that cannot be used yet is on show.
    print("\nfirst screen")
    check("with nothing open, the welcome screen is showing",
          win.center.currentWidget() is win.welcome)
    check("it offers a folder and a project",
          win.welcome.btn_folder.isVisible() and win.welcome.btn_project.isVisible())
    check("the options bar is not on show with nothing to describe",
          not win.options_bar.isVisible())
    check("the tools are off until there is a page",
          not win.tool_column.isEnabled() and not win.a_tool_crop.isEnabled())
    check("and so are save and export",
          not win.a_save.isEnabled() and not win.a_export.isEnabled())
    check("the empty page grid says what it is for",
          "pages appear here" in win.queue.list.empty_text.lower())
    check("two toolbars", win.toolbar_main is not None and win.toolbar_tools is not None)
    main_actions = [a.text().replace("&", "") for a in win.toolbar_main.actions()
                    if not a.isSeparator()]
    check("main bar order",
          main_actions == ["Open scan folder…", "Save project", "Save project as…",
                           "Export…", "Undo", "Redo",
                           "Add horizontal guide", "Add vertical guide",
                           "Clear guides", "Show grid",
                           "Preview as a book…"],
          str(main_actions))
    check("main bar is icons only",
          win.toolbar_main.toolButtonStyle() == Qt.ToolButtonIconOnly)
    check("tools live on their own bar",
          any(a is win.a_tool_crop for a in win.toolbar_tools.actions()))

    # ---------------------------------------------- drag and drop a folder
    print("\ndrop a folder")
    # The modal has to be up before anything is read.  It used to come last:
    # the queue was rebuilt first, which asked for a thumbnail per page, which
    # started decoding the whole folder with the window still live - the exact
    # thing the modal exists to prevent, happening before the modal appeared.
    seen = {"dialog_up": False, "rows": 0, "decodes_before": 0, "queue": 0,
            "importing": False}
    real_build = proxycache.build

    def watched_build(path, *a, **kw):
        # asked of the window directly rather than of a polling timer, so this
        # is the real state at the moment the decode begins
        if seen["importing"] and win._load_dialog is None:
            seen["decodes_before"] += 1
        return real_build(path, *a, **kw)

    real_import = win._import

    def watched_import(added):
        seen["queue"] = win.queue.list.count()
        real_import(added)

    def peek():
        dlg = win._load_dialog
        if dlg is not None:
            seen["dialog_up"] = True
            seen["rows"] = dlg.list.count()

    proxycache.build = watched_build
    win._import = watched_import
    from PySide6.QtCore import QTimer
    watcher = QTimer()
    watcher.setInterval(10)
    watcher.timeout.connect(peek)
    watcher.start()

    seen["importing"] = True
    win.add_paths([scans])            # what dropEvent calls
    seen["importing"] = False
    pump()
    watcher.stop()
    proxycache.build = real_build
    del win._import

    check("the loading modal appears for the import", seen["dialog_up"])
    check("it lists every page being read", seen["rows"] == 4, str(seen["rows"]))
    check("nothing is decoded before it is up",
          seen["decodes_before"] == 0,
          f"{seen['decodes_before']} decodes started first")
    check("and the queue is not filled behind it either",
          seen["queue"] == 0, f"{seen['queue']} rows already there")
    check("the modal is gone once the import is done",
          win._load_dialog is None)

    check("4 pages queued", len(win.project.pages) == 4, str(len(win.project.pages)))
    check("the queue is filled once the import finishes",
          win.queue.list.count() == 4, str(win.queue.list.count()))

    # Thumbnails must not be built while the list is being built.  Each one is
    # a JPEG decode, a scale and a paint on the interface thread - about three
    # milliseconds - so doing all of them inline froze the window for a second
    # on a 300-page folder and several on a big one, every single rebuild.
    from scanforge.ui.queue_panel import _ICON_ROLE
    win.queue.forget_thumbs()
    t0 = time.time()
    win.queue.rebuild()
    build_ms = (time.time() - t0) * 1000
    fresh = [win.queue.list.item(i).data(_ICON_ROLE)
             for i in range(win.queue.list.count())]
    check("rebuilding the queue draws no thumbnails itself",
          not any(fresh), str(fresh))
    check("so it returns immediately", build_ms < 200, f"{build_ms:.0f} ms")
    check("and the filler is left to do the pictures",
          win.queue._icon_timer.isActive())
    wait_until(lambda: not win.queue._icon_timer.isActive(), 20)
    later = [win.queue.list.item(i).data(_ICON_ROLE)
             for i in range(win.queue.list.count())]
    check("which it does, for the rows on screen", all(later), str(later))
    check("every page was read", all(proxycache.has(p.path, "proxy")
                                     for p in win.project.pages))
    check("working folder derived from the drop",
          win.project.last_folder == scans, win.project.last_folder)
    check("export folder is beside the scans, not inside them",
          win.export_cfg.out_dir == os.path.dirname(scans),
          win.export_cfg.out_dir)
    check("document name inherited from the folder",
          win.project.document_name == os.path.basename(scans),
          win.project.document_name)
    check("window title shows it",
          os.path.basename(scans) in win.windowTitle(), win.windowTitle())

    # dropping a second folder must ask, not silently merge
    asked = []
    win._ask_new_or_add = lambda paths: (asked.append(paths) or False)
    other = os.path.join(TMP, "more")
    make_pages(other, 2)
    win.add_paths([other])
    wait_until(lambda: not win._busy, 30)
    check("a second drop asks what to do", len(asked) == 1, str(len(asked)))
    check("choosing 'add' keeps the existing pages",
          len(win.project.pages) == 6, str(len(win.project.pages)))

    win._ask_new_or_add = lambda paths: True          # 'start a new project'
    win._dirty = False
    win.add_paths([other])
    wait_until(lambda: not win._busy, 30)
    check("choosing 'new' replaces the project",
          len(win.project.pages) == 2, str(len(win.project.pages)))
    check("and re-inherits the new folder's name",
          win.project.document_name == "more", win.project.document_name)

    # back to the real fixture for the rest of the run
    win._dirty = False
    win.new_project(ask=False)
    check("new_project empties everything",
          not win.project.pages and not win.project.document_name)
    del win._ask_new_or_add
    win.add_paths([scans])
    wait_until(lambda: not win._busy, 30)
    check("fixture reloaded", len(win.project.pages) == 4,
          str(len(win.project.pages)))

    win.queue.select(win.project.pages[0].uid)
    check("page loads without blocking the UI",
          wait_until(lambda: win._proxy is not None), "timed out")
    check("with pages open, the canvas replaces the welcome screen",
          win.center.currentWidget() is win.canvas)
    check("and the options bar and tools come on",
          win.options_bar.isVisible() and win.tool_column.isEnabled()
          and win.a_export.isEnabled())
    check("the status bar names the page and what it is made of",
          win.project.pages[0].name in win.lbl_page.text()
          and "dpi" in win.lbl_page.text(), win.lbl_page.text())
    check("queue rebuilt", win.queue.list.count() == 4, str(win.queue.list.count()))
    check("canvas paints something", nonblank(paint(win.canvas)))

    # ---------------------------------------------------- the two crop tools
    print("\ncrop page, crop all pages")
    check("a fresh project starts on the cursor, not on a tool",
          win.canvas.tool == Tool.CURSOR, str(win.canvas.tool))
    check("nothing arms itself and nothing is pre-cropped",
          all(p.crop is None for p in win.project.pages))
    check("the project-wide crop toggle is gone",
          not hasattr(win, "a_link") and not hasattr(win, "toggle_link")
          and not hasattr(win.project, "link_crop"))

    e0 = win._current
    c0 = win.canvas

    def drag_crop(canvas, a, b):
        canvas.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(*a),
                                           Qt.LeftButton, Qt.LeftButton,
                                           Qt.NoModifier))
        for t in (0.35, 0.7, 1.0):
            canvas.mouseMoveEvent(QMouseEvent(
                QEvent.MouseMove,
                QPointF(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t),
                Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        canvas.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease,
                                             QPointF(*b), Qt.LeftButton,
                                             Qt.NoButton, Qt.NoModifier))
        pump()

    # A click with no drag takes the whole page, rather than committing a
    # one-pixel crop or doing nothing at all.  Most pages are cropped by
    # pulling a full-page frame in, and that used to need a drag from one
    # corner of the scan to the other before you could start.
    win.set_tool(Tool.CROP)
    check("the crop tool arms when asked", win.canvas.tool == Tool.CROP,
          str(win.canvas.tool))
    c0.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(200, 200),
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    c0.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(201, 201),
                                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    W0, H0 = c0.page_canvas()
    got = win._current.crop
    check("a click with the crop tool takes the whole page",
          got is not None
          and abs(got[2] * c0._scale - W0) < 2
          and abs(got[3] * c0._scale - H0) < 2,
          str(got))
    check("and it is not a one-pixel crop",
          got is not None and got[2] > 50 and got[3] > 50, str(got))
    check("the tool releases itself, as a one-shot tool does",
          win.canvas.tool == Tool.CURSOR, str(win.canvas.tool))
    win.clear_crop()
    pump()

    # ---- a crop stays inside the scan
    # A crop that reaches past the edge of the scan exports scanner bed, or
    # nothing at all, and is nearly always a slip rather than an intention.
    Wp, Hp = c0.page_canvas()
    inside = QRectF(0.0, 0.0, Wp, Hp)
    over = QRectF(-200.0, -150.0, Wp + 400.0, Hp + 300.0)
    fixed = c0.confine_crop(over)
    check("a crop drawn past the edge is brought back inside",
          inside.contains(fixed) and fixed.width() > Wp * 0.9,
          f"{fixed.width():.0f}x{fixed.height():.0f} in {Wp:.0f}x{Hp:.0f}")
    moved = c0.confine_crop(QRectF(Wp - 40.0, Hp - 40.0, 300.0, 400.0), "move")
    check("a crop dragged off the edge slides back, same size",
          inside.contains(moved) and abs(moved.width() - 300.0) < 1e-6
          and abs(moved.height() - 400.0) < 1e-6,
          f"{moved}")

    win.set_tool(Tool.CROP)
    c0.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(60, 60),
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    far = QPointF(c0.width() + 400, c0.height() + 400)
    c0.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, far, Qt.NoButton,
                                  Qt.LeftButton, Qt.NoModifier))
    pump()
    drawn = c0.crop_scene()
    check("dragging out past the scan clamps as you go",
          drawn.right() <= Wp + 0.5 and drawn.bottom() <= Hp + 0.5,
          f"{drawn.right():.0f},{drawn.bottom():.0f} vs {Wp:.0f},{Hp:.0f}")
    # Shift is the way out, for the scan that genuinely needs framing past
    # its own edge
    c0.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, far, Qt.NoButton,
                                  Qt.LeftButton, Qt.ShiftModifier))
    pump()
    loose = c0.crop_scene()
    check("holding Shift lets it out again",
          loose.right() > Wp + 1 or loose.bottom() > Hp + 1,
          f"{loose.right():.0f},{loose.bottom():.0f}")
    c0.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, far,
                                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    win.clear_crop()
    pump()

    # "crop page" reaches exactly one page.  The tool has to be armed again:
    # the click above consumed the arming, which is what a one-shot tool does.
    win.set_tool(Tool.CROP)
    drag_crop(c0, (80, 80), (340, 420))
    cropped = [p for p in win.project.pages if p.crop]
    check("crop page crops the page you are on", win._current.crop is not None)
    check("and no other page", len(cropped) == 1,
          f"{len(cropped)} pages have a crop")
    check("the tool releases itself afterwards",
          win.canvas.tool == Tool.CURSOR, str(win.canvas.tool))
    kept = tuple(round(v) for v in win._current.crop)

    # "crop all pages" reaches the whole document in one drag
    win.set_tool(Tool.CROP_ALL)
    check("the crop-all tool arms", win.canvas.tool == Tool.CROP_ALL,
          str(win.canvas.tool))
    drag_crop(c0, (100, 110), (360, 440))
    crops = {tuple(round(v) for v in p.crop)
             for p in win.project.pages if p.crop}
    check("crop all pages gives every page the same crop",
          len(crops) == 1 and len([p for p in win.project.pages if p.crop])
          == len(win.project.pages), f"{len(crops)} distinct crops")
    check("and it is a real rectangle, not a stray pixel",
          all(c[2] > 50 and c[3] > 50 for c in crops), str(crops))
    check("it is the rectangle just drawn, not the earlier one",
          crops != {kept} and all(c[2] < W0 / c0._scale - 5 for c in crops),
          f"{crops} vs {kept}")
    check("it releases itself too", win.canvas.tool == Tool.CURSOR,
          str(win.canvas.tool))
    check("and says how far it reached",
          "pages" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())

    # nothing propagates unless you asked for it
    win.queue.select(win.project.pages[2].uid)
    wait_until(lambda: win._proxy is not None)
    others = [tuple(p.crop) for p in win.project.pages if p is not win._current]
    win.inspector.spins["x"].setValue(310)
    pump()
    check("editing a crop afterwards stays on its own page",
          [tuple(p.crop) for p in win.project.pages
           if p is not win._current] == others, "other pages moved")

    # a locked page sits out of crop-all in both directions
    win.queue.select(win.project.pages[1].uid)
    wait_until(lambda: win._proxy is not None)
    win._current.crop_mode = "locked"
    locked_before = tuple(win._current.crop)
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.set_tool(Tool.CROP_ALL)
    drag_crop(win.canvas, (90, 100), (330, 400))
    check("crop all pages leaves a locked page alone",
          tuple(win.project.pages[1].crop) == locked_before,
          f"{win.project.pages[1].crop} vs {list(locked_before)}")
    win.project.pages[1].crop_mode = "linked"

    # a click or a pan is not an edit and must reach nothing
    before_click = [tuple(p.crop) for p in win.project.pages]
    c0.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(300, 300),
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    c0.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(300, 300),
                                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    check("clicking the canvas reshapes nothing",
          [tuple(p.crop) for p in win.project.pages] == before_click,
          "a click propagated the crop")

    # ---- crop-all has the crop tool's cursor
    # It draws the very same rectangle, but the cursor check named Tool.CROP
    # only, so crop-all fell through to the open hand.
    for tool in (Tool.CROP, Tool.CROP_ALL):
        win.set_tool(tool)
        c0.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(25, 25),
                                      Qt.NoButton, Qt.NoButton, Qt.NoModifier))
        check(f"{tool.name.lower()} shows the crosshair",
              c0.cursor().shape() == Qt.CrossCursor, str(c0.cursor().shape()))
    win.set_tool(Tool.CURSOR)

    # ---- "edit all pages with this crop"
    print("\nlinked crop edits")
    pg = win.project.pages
    win.queue.select(pg[0].uid)
    wait_until(lambda: win._proxy is not None)
    same = [300.0, 400.0, 1500.0, 2200.0]
    pg[0].crop = list(same)
    pg[1].crop = list(same)
    # straightening moves a page's crop with its content, so a document that
    # was cropped all at once is the same *size* everywhere but not always the
    # same rectangle - that page has to stay in the group
    pg[2].crop = [same[0] + 20, same[1] + 10, same[2], same[3]]
    pg[3].crop = list(same)
    pg[3].crop_mode = "locked"
    for p_ in pg:
        if p_ is not pg[3]:
            p_.crop_mode = "linked"
    win._reload_current()
    check("the toggle is off to begin with", not win.a_link_crop.isChecked())
    check("and while it is off the frame claims no other pages",
          win.canvas.linked_count == 0, str(win.canvas.linked_count))

    win.a_link_crop.trigger()
    pump()
    check("switching it on counts the pages with the same crop size",
          win.canvas.linked_count == 2, str(win.canvas.linked_count))
    check("and says so", "2 other pages" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())

    locked_before = list(pg[3].crop)
    b1, b2 = list(pg[1].crop), list(pg[2].crop)
    # pull the left edge out by 30 px, through the crop numbers
    win._set_crop(same[0] - 30, same[1], same[2] + 30, same[3])
    win._end_history()
    pump()
    check("the edit is made on the matching page",
          pg[1].crop == [b1[0] - 30, b1[1], b1[2] + 30, b1[3]], str(pg[1].crop))
    check("and on the straightened one, from its own position",
          pg[2].crop == [b2[0] - 30, b2[1], b2[2] + 30, b2[3]], str(pg[2].crop))
    check("a locked page is left alone", pg[3].crop == locked_before,
          str(pg[3].crop))
    check("the status bar says how far it reached",
          "3 pages" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())
    check("and the group is still the same pages afterwards",
          win.canvas.linked_count == 2, str(win.canvas.linked_count))

    # the gesture itself: drag the right-hand handle in
    c = win.canvas
    r = c.crop_scene()
    grab = c.mapFromScene(QPointF(r.right(), r.center().y()))
    b1 = list(pg[1].crop)
    c.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(grab),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    c.mouseMoveEvent(QMouseEvent(QEvent.MouseMove,
                                 QPointF(grab.x() - 40, grab.y()),
                                 Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    c.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease,
                                    QPointF(grab.x() - 40, grab.y()),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    dw = pg[0].crop[2] - (same[2] + 30)
    check("dragging a handle reaches the group too",
          dw < -5 and abs(pg[1].crop[2] - (b1[2] + dw)) < 0.01
          and pg[1].crop[0] == b1[0],
          f"this page width moved {dw:.1f}; other page {b1} -> {pg[1].crop}")

    # one undo takes back the whole edit, on every page it reached
    reached = [list(pg[1].crop), list(pg[2].crop)]
    win.undo()
    pump()
    check("undo takes it back on every page at once",
          [list(pg[1].crop), list(pg[2].crop)] != reached
          and abs(pg[0].crop[2] - pg[1].crop[2]) < 0.01,
          f"{pg[0].crop} / {pg[1].crop}")

    # straightening moves this page's crop with its content - not the others'
    b1 = list(pg[1].crop)
    win._set_angle(0.25)
    win._end_history()
    pump()
    check("straightening a page does not move the other pages' crops",
          pg[1].crop == b1, f"{b1} -> {pg[1].crop}")
    win._set_angle(0.0)
    win._end_history()
    pump()

    # and with the toggle off, an edit is this page's alone again
    win.a_link_crop.trigger()
    pump()
    b1 = list(pg[1].crop)
    win._set_crop(pg[0].crop[0] + 12, pg[0].crop[1], pg[0].crop[2] - 12,
                  pg[0].crop[3])
    win._end_history()
    pump()
    check("switched off, an edit stays on its own page", pg[1].crop == b1,
          f"{b1} -> {pg[1].crop}")
    check("and the frame stops claiming other pages",
          win.canvas.linked_count == 0, str(win.canvas.linked_count))
    pg[3].crop_mode = "linked"


    check("the status bar has a spinner", hasattr(win, "throbber"))
    win._claim("demo", "working")
    check("the spinner runs while something is busy", win.throbber.spinning)
    win._release("demo")
    check("and stops when nothing is", not win.throbber.spinning)

    win.notice.show_notice("Testing", "body text", spinning=True)
    check("the canvas notice shows", win.notice.isVisible())
    check("with its own spinner", win.notice.throbber.spinning)
    win.notice.dismiss()
    check("and dismisses", not win.notice.isVisible()
          and not win.notice.throbber.spinning)

    # the modal that owns the window while a queue is read
    from scanforge.ui.loading_dialog import (QueueLoadDialog, QUEUED, READING,
                                             READY, FAILED, _STATE_ROLE)
    from scanforge.model import PageEdit as _PE
    from PySide6.QtGui import QCloseEvent
    fake = [_PE(path=f"/tmp/fake_{i}.tif") for i in range(3)]
    dlg = QueueLoadDialog(fake, win)
    dlg.show()          # shown, not exec()'d, so the test can drive it
    pump()
    check("it is modal", dlg.isModal())
    check("it has no close button",
          not (dlg.windowFlags() & Qt.WindowCloseButtonHint))
    check("it lists every page", dlg.list.count() == 3, str(dlg.list.count()))
    check("all start as waiting",
          all(dlg.list.item(i).data(_STATE_ROLE) == QUEUED for i in range(3)))

    dlg.mark_start("/tmp/fake_0.tif")
    check("a page being read shows as reading",
          dlg.list.item(0).data(_STATE_ROLE) == READING)
    check("its neighbours still wait",
          dlg.list.item(1).data(_STATE_ROLE) == QUEUED)
    dlg.mark_done("/tmp/fake_0.tif")
    check("a finished page shows as ready",
          dlg.list.item(0).data(_STATE_ROLE) == READY)
    check("the overall bar advances", dlg.bar.value() == 1, str(dlg.bar.value()))

    # it must refuse every way out until the work is done
    check("it is on screen to begin with", dlg.isVisible())
    dlg.reject()
    pump()
    check("Escape and Cancel are refused while loading", dlg.isVisible())
    ev = QCloseEvent()
    ev.accept()
    dlg.closeEvent(ev)
    check("the close event is refused too",
          not ev.isAccepted() and dlg.isVisible(),
          f"accepted={ev.isAccepted()} visible={dlg.isVisible()}")
    dlg.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    pump()
    check("Escape by key is refused as well", dlg.isVisible())

    dlg.mark_done("/tmp/fake_1.tif", ok=False)
    check("an unreadable page is marked, not silently skipped",
          dlg.list.item(1).data(_STATE_ROLE) == FAILED)
    check("and still counts towards finishing", dlg.bar.value() == 2,
          str(dlg.bar.value()))
    dlg.mark_done("/tmp/fake_2.tif")
    check("the dialog closes itself once every page is accounted for",
          not dlg.isVisible())

    # A modal with no close button is only defensible while finishing is
    # certain, and reading a disk is never certain.  So it watches itself: when
    # progress stops for long enough it stops claiming to be working and offers
    # a way out with whatever did load.
    stall = QueueLoadDialog(fake, win)
    stall.stall_seconds = 0.0
    # isHidden rather than isVisible: the dialog itself is not shown in the
    # test, and a child of a hidden parent is never "visible" either way
    check("no way out while it is working", stall.btn_give_up.isHidden())
    stall.mark_start("/tmp/fake_0.tif")
    stall._check_stalled()
    check("a stalled import offers one", not stall.btn_give_up.isHidden())
    check("and says what is wrong",
          "stopped making progress" in stall.title.text(), stall.title.text())
    check("it is not finished until the button is used", not stall.is_finished)
    stall._give_up()
    check("taking it ends the wait", stall.is_finished and stall.gave_up)
    check("and it reports what never arrived",
          len(stall.unread_paths()) == 3, str(stall.unread_paths()))
    stall.close()

    # progress must reset the stall clock, or a slow-but-working import would
    # start offering an exit halfway through
    live = QueueLoadDialog(fake, win)
    live.stall_seconds = 30.0
    live.mark_done("/tmp/fake_0.tif")
    live._check_stalled()
    check("a working import never offers a way out",
          live.btn_give_up.isHidden())
    live.close()

    # A dialog that finished before its loop started must not be exec()'d:
    # accept() with no loop running has nothing to exit, so exec() would enter
    # one that nothing can leave - behind a dialog with no close button.
    dlg3 = QueueLoadDialog(fake[:1], win)
    check("a fresh dialog knows it is not finished", not dlg3.is_finished)
    dlg3.mark_done("/tmp/fake_0.tif")
    check("and says so once every page is accounted for", dlg3.is_finished)
    dlg3.close()

    # the percentage is real: the decoder reports how far through the scan it
    # is, tile by tile, so the bar fills rather than merely animating
    from scanforge.ui.loading_dialog import _PROG_ROLE
    pdlg = QueueLoadDialog(fake, win)
    check("a page starts with nothing to report",
          pdlg.list.item(0).data(_PROG_ROLE) is None)
    pdlg.mark_progress("/tmp/fake_0.tif", 0.42)
    check("and then reports a real fraction",
          abs(pdlg.list.item(0).data(_PROG_ROLE) - 0.42) < 1e-6
          and pdlg.list.item(0).data(_STATE_ROLE) == READING,
          str(pdlg.list.item(0).data(_PROG_ROLE)))
    pdlg.mark_done("/tmp/fake_0.tif")
    pdlg.mark_progress("/tmp/fake_0.tif", 0.9)
    check("a finished page is not dragged backwards by a late report",
          pdlg.list.item(0).data(_STATE_ROLE) == READY)
    check("cancelling is offered from the start, unlike the failure exit",
          not pdlg.btn_cancel.isHidden() and pdlg.btn_give_up.isHidden())
    pdlg._cancel()
    check("and it ends the wait, saying it was cancelled",
          pdlg.is_finished and pdlg.cancelled)
    pdlg.close()

    # a thumbnail finishing must not mark a page ready before its proxy
    dlg2 = QueueLoadDialog(fake[:1], win)
    win._load_dialog = dlg2
    win._cache_finished("/tmp/fake_0.tif", ("thumb",), True)
    check("a thumbnail completion does not count",
          dlg2.list.item(0).data(_STATE_ROLE) == QUEUED
          and dlg2.bar.value() == 0, str(dlg2.bar.value()))
    win._cache_finished("/tmp/fake_0.tif", ("thumb", "proxy"), True)
    check("the proxy completion does", dlg2.bar.value() == 1, str(dlg2.bar.value()))
    win._load_dialog = None
    dlg2.close()

    # ------------------------------------------------------------- panning
    print("\npanning")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.canvas.zoom_to(0.5)
    pump()
    hs = win.canvas.horizontalScrollBar()
    c0.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(400, 400),
                                   Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier))
    check("middle-drag starts a pan", win.canvas._drag
          and win.canvas._drag[0] == "pan", str(win.canvas._drag))
    start = hs.value()
    # twelve sub-pixel moves: truncating each one threw the movement away and
    # the view refused to budge, which is what the hitching was
    for i in range(12):
        c0.mouseMoveEvent(QMouseEvent(QEvent.MouseMove,
                                      QPointF(400 - (i + 1) * 0.4, 400),
                                      Qt.NoButton, Qt.MiddleButton, Qt.NoModifier))
    moved = hs.value() - start
    check("sub-pixel drags accumulate instead of being dropped",
          moved >= 4, f"scrolled {moved} px over 4.8 px of movement")
    c0.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(395, 400),
                                     Qt.MiddleButton, Qt.NoButton, Qt.NoModifier))
    win.canvas.fit()
    pump()

    # ------------------------------------------------------------- tools
    print("\ntool modes")
    win.canvas.tool = Tool.CURSOR
    check("starts in cursor mode", win.canvas.tool == Tool.CURSOR)
    e = win._current
    win.canvas.select_all()
    pump()
    original = list(e.crop)

    # a stray drag in cursor mode must not start a new crop.  The point has to
    # be worked out from where the crop actually is rather than guessed at a
    # fixed corner - the canvas is a different width now that the tools live
    # beside it, and a hardcoded corner quietly ended up inside the page.
    c = win.canvas
    outside = c.mapFromScene(QPointF(c.crop_scene().left() - 40,
                                     c.crop_scene().center().y()))
    check("the probe point really is outside the crop, and on screen",
          not c.crop_scene().contains(c.mapToScene(outside))
          and c.viewport().rect().contains(outside),
          f"{outside.x()},{outside.y()} in {c.viewport().width()}"
          f"x{c.viewport().height()}")
    press = QMouseEvent(QEvent.MouseButtonPress, QPointF(outside), Qt.LeftButton,
                        Qt.LeftButton, Qt.NoModifier)
    c.mousePressEvent(press)
    kind = c._drag[0] if c._drag else None
    c.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(200, 200),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    check("cursor mode pans instead of re-cropping", kind == "pan", str(kind))
    check("existing crop untouched", e.crop == original)

    win.set_tool(Tool.CROP)
    check("crop tool arms", win.canvas.tool == Tool.CROP)
    c.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(60, 60),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    kind = c._drag[0] if c._drag else None
    for x, y in ((160, 160), (300, 300)):
        c.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y),
                                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    c.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(300, 300),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    check("crop tool draws", kind == "new", str(kind))
    check("crop tool releases itself after one use",
          win.canvas.tool == Tool.CURSOR, str(win.canvas.tool))
    check("toolbar reflects the release", win.a_tool_cursor.isChecked()
          and not win.a_tool_crop.isChecked())

    win.set_tool(Tool.BLUR)
    win.escape()
    check("Esc returns to cursor", win.canvas.tool == Tool.CURSOR)

    def shown(a):
        """The name as the app shows it: "&&" is a literal ampersand."""
        return a.text().replace("&&", "\0").replace("&", "").replace("\0", "&")
    names = [shown(a) for a in win.toolbar_tools.actions()
             if not a.isSeparator()]
    check("the tool column holds the editing tools, in groups",
          names == ["Cursor", "Crop page", "Crop all pages",
                    "Edit all pages with this crop",
                    "Blur a spot", "Cut into pages",
                    "Rotate 90° left", "Rotate 90° right",
                    "Flip horizontally", "Flip vertically",
                    "Copy framing from previous",
                    "Match crop sizes…", "Align or join two pages…",
                    "Auto straighten & crop", "Fit crop to page edges",
                    "Mark done and go to next"], str(names))
    check("with a rule between each group",
          sum(1 for a in win.toolbar_tools.actions() if a.isSeparator()) == 4,
          str(sum(1 for a in win.toolbar_tools.actions() if a.isSeparator())))

    top = [a.text().replace("&", "") for a in win.toolbar_main.actions()
           if not a.isSeparator()]
    check("and the top bar is the document and the view, nothing per-page",
          top == ["Open scan folder…", "Save project", "Save project as…",
                  "Export…", "Undo", "Redo",
                  "Add horizontal guide", "Add vertical guide", "Clear guides",
                  "Show grid", "Preview as a book…"], str(top))

    # the column sits between the page grid and the canvas, which is the whole
    # point of it - the tools are next to the thing they act on
    shell = [win.work_layout.itemAt(i).widget()
             for i in range(win.work_layout.count())]
    check("the tool column is the leftmost thing in the window",
          shell[0] is win.tool_column and shell[1] is win.splitter,
          str([type(w).__name__ for w in shell]))
    order = [win.splitter.widget(i) for i in range(win.splitter.count())]
    check("then the editing area, then the pages",
          order == [win.editing, win.queue],
          str([type(w).__name__ for w in order]))
    # The right-hand panel is gone: its repeats of the tool column went, and
    # what was left moved into the options bar above the canvas.
    ed = [win.editing.layout().itemAt(i).widget()
          for i in range(win.editing.layout().count())]
    check("the editing area is the options bar, then the canvas",
          ed == [win.options_bar, win.center],
          str([type(w).__name__ for w in ed]))
    check("the angle controls live in the options bar",
          win.angle_strip.parent() is win.options_bar)
    check("the page grid has the whole right-hand column",
          win.queue.height() >= win.splitter.height() - 4,
          f"grid {win.queue.height()} of {win.splitter.height()} px")

    # hover shows the name and the key; the status bar says what it does
    for a in (win.a_tool_crop, win.a_link_crop, win.a_auto_both, win.a_spread):
        check(f"{shown(a)}: the tooltip is its name and key",
              a.toolTip().startswith(shown(a))
              and a.shortcut().toString() in a.toolTip(), a.toolTip())
        check(f"{shown(a)}: the status bar gets a description, not the name again",
              a.statusTip() and a.statusTip() != shown(a)
              and len(a.statusTip()) > len(shown(a)) + 10, a.statusTip())
    check("and nothing sits to the right of the pages",
          win.splitter.count() == 2, str(win.splitter.count()))

    # ---- the shortcuts sheet covers every shortcut the window really has
    # F1 used to be a block of plain text that had drifted: it still listed a
    # key for a feature that had been removed, and left out others.  It is a
    # structured list now, checked against the actions themselves.
    from PySide6.QtGui import QAction, QKeySequence
    from scanforge.ui.help_dialog import SECTIONS, HelpDialog
    arrows = {"←": "Left", "→": "Right", "↑": "Up", "↓": "Down"}

    def norm(k):
        for a_, b_ in arrows.items():
            k = k.replace(a_, b_)
        return QKeySequence(k.replace("Enter", "Return")).toString()
    documented = set()
    for _title, rows in SECTIONS:
        for keys, _what in rows:
            parts = [x.strip() for x in keys.split(" / ")]
            base = parts[0].rsplit("+", 1)[0] + "+" if "+" in parts[0] else ""
            for n_, part in enumerate(parts):
                if n_ and "+" not in part and base:
                    part = base + part           # "Ctrl+Shift+↑ / ↓"
                documented.add(norm(part))
    aliases = {"Ctrl+=", "Ctrl+Shift+Z", "Ctrl+Q"}   # second keys, and quit
    undocumented = sorted({(ks.toString(), a.text().replace("&", ""))
                           for a in win.findChildren(QAction)
                           for ks in a.shortcuts()
                           if ks.toString() and ks.toString() not in documented
                           and ks.toString() not in aliases})
    check("every shortcut in the app is on the F1 sheet", not undocumented,
          str(undocumented))
    live = {ks.toString() for a in win.findChildren(QAction) for ks in a.shortcuts()}
    canvas_keys = {"Esc", "Space", "Shift+Space", "Del", "Return", "Ctrl+Return"}
    stale = sorted(k for k in documented
                   if len(k) <= 22 and "+" in k or len(k) == 1
                   if k and k not in live and k not in canvas_keys
                   and k not in {"[", "]"})
    check("and nothing on it is a key the app no longer has", not stale,
          str(stale))
    hd = HelpDialog(win)
    check("the sheet opens", hd.windowTitle() == "Keyboard shortcuts")
    hd.deleteLater()

    # --------------------------------------------------------- orientation
    print("\norientation")
    # let any background probing settle first, or its queue rebuild can land
    # in the middle of this section and swap the page out from under it
    wait_until(lambda: not win._busy, 30)
    win.queue.select(win.project.pages[0].uid)
    check("a page is loaded to work on",
          wait_until(lambda: win._proxy is not None, 30), "timed out")
    page = win._current
    win.reset_page()
    pump()
    upright = win._proxy.copy()
    ref_up = win.canvas._ref_size
    scale_up = win.canvas._scale
    size_up = page.output_size()
    check("starts upright", page.quadrant == 0 and not page.flip_h)

    win._set_orientation(1, False, False)
    pump()
    check("90 degrees actually turns the preview",
          win._proxy.shape[:2] == upright.shape[:2][::-1],
          f"{upright.shape[:2]} -> {win._proxy.shape[:2]}")
    check("the turned preview is the source rotated, not just relabelled",
          np.array_equal(win._proxy, np.rot90(upright, k=-1)))
    check("canvas reference size follows the turn",
          win.canvas._ref_size == ref_up[::-1],
          f"{ref_up} -> {win.canvas._ref_size}")
    # the proxy's integer dimensions round differently on each axis, so allow
    # a whisker; the bug this guards against made the scale wrong by ~30%
    check("proxy scale stays correct through a turn",
          abs(win.canvas._scale - scale_up) / scale_up < 1e-3,
          f"{scale_up:.5f} -> {win.canvas._scale:.5f}")
    check("output size swaps", page.output_size() == size_up[::-1],
          f"{size_up} -> {page.output_size()}")
    check("canvas still paints", nonblank(paint(win.canvas)))

    win._set_orientation(2, False, False)
    pump()
    check("180 degrees", np.array_equal(win._proxy, np.rot90(upright, k=-2)))
    win._set_orientation(3, False, False)
    pump()
    check("270 degrees", np.array_equal(win._proxy, np.rot90(upright, k=-3)))
    win._set_orientation(0, False, False)
    pump()
    check("four turns come back to upright", np.array_equal(win._proxy, upright))
    check("and so does the output size", page.output_size() == size_up)

    win._set_orientation(0, True, False)
    pump()
    check("flip across mirrors the preview",
          np.array_equal(win._proxy, upright[:, ::-1]))
    win._set_orientation(0, True, True)
    pump()
    check("flip down mirrors it too",
          np.array_equal(win._proxy, upright[::-1, ::-1]))
    win._set_orientation(0, False, False)
    pump()

    # the crop has to travel with the page, not stay put in the frame
    win.canvas.select_all()
    pump()
    W0, H0 = page.output_size()
    page.crop = [100.0, 250.0, 1400.0, 2000.0]
    win._set_orientation(1, False, False)
    pump()
    x, y, w, h = page.crop
    check("a quadrant turn swaps the crop's own dimensions",
          (round(w), round(h)) == (2000, 1400), f"{round(w)}x{round(h)}")
    check("and moves it to the matching corner",
          abs(x - (H0 - (250.0 + 2000.0))) < 1.5 and abs(y - 100.0) < 1.5,
          f"crop now at {x:.0f},{y:.0f}")
    win._set_orientation(0, False, False)
    pump()
    check("turning back restores the crop",
          [round(v) for v in page.crop] == [100, 250, 1400, 2000],
          str([round(v) for v in page.crop]))

    page.crop = [100.0, 250.0, 1400.0, 2000.0]
    Wc, Hc = page.output_size()[0], page.output_size()[1]
    win._set_orientation(0, True, False)
    pump()
    check("a flip mirrors the crop across the page",
          abs(page.crop[0] - (2400.0 - (100.0 + 1400.0))) < 1.5,
          f"x is {page.crop[0]:.0f}, expected {2400.0 - 1500.0:.0f}")
    win._set_orientation(0, False, False)
    pump()

    n_hist = len(win.history._undo)
    win._set_orientation(1, False, False)
    pump()
    check("orientation is undoable", len(win.history._undo) > n_hist)
    win.undo()
    pump()
    check("undo puts the page back upright",
          win.project.pages[0].quadrant == 0, str(win.project.pages[0].quadrant))
    check("and the preview follows the undo",
          win._proxy is not None and np.array_equal(win._proxy, upright))
    win.reset_page()
    pump()

    # ---------------------------------------------------------------- blur
    print("\nblur tool")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    page = win._current
    page.blurs.clear()
    # The first blur of a session opens a modal dialog to ask how it should
    # look.  Record that it was asked for, then answer it here rather than
    # letting exec() sit there waiting for a person.
    asked = []
    win.canvas.blurPlaced.connect(lambda i: asked.append(i))
    win._blur_asked = True
    win.set_tool(Tool.BLUR)
    check("blur tool arms", win.canvas.tool == Tool.BLUR)
    c.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(120, 120),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    kind = c._drag[0] if c._drag else None
    c.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(320, 300),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    check("blur drag creates a region", kind == "blur_new" and len(page.blurs) == 1,
          f"{kind}, {len(page.blurs)} blurs")
    check("blur tool releases itself", win.canvas.tool == Tool.CURSOR)
    check("page counts as edited", page.is_edited())
    check("placing a blur asks for its settings", asked == [0], str(asked))
    check("the inspector shows which blur is selected",
          win.inspector.btn_blur_edit.isEnabled())

    # ---- defaults, and where they come from
    from scanforge.ui.blur_dialog import (BlurDialog, DEFAULT_STRENGTH,
                                          DEFAULT_STYLE)
    b = page.blurs[0]
    check("a new blur is pixelate at 100 by default",
          b[5] == "Pixelate" and abs(b[4] - 100.0) < 1e-6,
          f"{b[5]}, {b[4]}")
    check("and those are the module defaults",
          DEFAULT_STYLE == "Pixelate" and DEFAULT_STRENGTH == 100.0,
          f"{DEFAULT_STYLE}, {DEFAULT_STRENGTH}")
    check("the strength no longer depends on the size of the rectangle",
          abs(b[4] - min(b[2], b[3]) * 0.18) > 1.0,
          f"{b[4]} vs {min(b[2], b[3]) * 0.18:.1f}")

    # the dialog itself: what it reads, what it reports, and its live preview
    dlg = BlurDialog("Blur", 40.0)
    check("the dialog opens on the blur's current settings",
          dlg.values() == (40.0, "Blur"), str(dlg.values()))
    seen = []
    dlg.changed.connect(lambda v, m: seen.append((v, m)))
    dlg.sld.setValue(180)
    check("moving the slider reports a change for the live preview",
          seen and seen[-1] == (180.0, "Blur"), str(seen[-1:]))
    check("and the spin box follows it", dlg.sp.value() == 180, str(dlg.sp.value()))
    dlg.rb_pixelate.setChecked(True)
    check("switching style reports it too",
          seen[-1] == (180.0, "Pixelate"), str(seen[-1:]))
    dlg.deleteLater()

    # double-clicking a blur is how you get back to the settings.  The window
    # answers that signal by opening a modal dialog, so stand in for it here.
    reopen = []
    c.blurConfigure.disconnect()
    c.blurConfigure.connect(lambda i: reopen.append(i))
    c.blur_selected = -1
    r = c.blur_scene(0)
    mid = c.mapFromScene(r.center())
    c.mouseDoubleClickEvent(QMouseEvent(
        QEvent.MouseButtonDblClick, QPointF(mid), Qt.LeftButton,
        Qt.LeftButton, Qt.NoModifier))
    pump()
    check("double-clicking a blur asks to configure it", reopen == [0],
          str(reopen))
    check("and selects it on the way", c.blur_selected == 0,
          str(c.blur_selected))
    c.blurConfigure.disconnect()
    c.blurConfigure.connect(lambda _i: win.configure_blur())

    c.blur_selected = 0
    c.set_blur_property(40.0, "Pixelate")
    pump()
    check("the canvas applies a style and strength",
          abs(page.blurs[0][4] - 40) < 1e-6 and page.blurs[0][5] == "Pixelate",
          str(page.blurs[0][4:]))

    before = list(page.blurs[0][:2])
    c.blur_selected = 0
    c.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
    pump()
    check("arrow keys nudge the selected blur",
          abs(page.blurs[0][0] - (before[0] + 1)) < 0.5, str(page.blurs[0][0]))

    # a blur must follow the content when the page is straightened
    cx_before = page.blurs[0][0] + page.blurs[0][2] / 2
    win.canvas.set_angle(page.angle - 1.5)
    pump()
    check("blur follows the content through a rotation",
          abs((page.blurs[0][0] + page.blurs[0][2] / 2) - cx_before) > 1.0,
          "blur centre did not move with the frame")
    win.canvas.set_angle(page.angle + 1.5)

    check("blur paints on the canvas", nonblank(paint(win.canvas)))

    n_hist = len(win.history._undo)
    c.delete_blur(0)
    pump()
    check("blur deleted", not page.blurs)
    check("deletion is undoable", len(win.history._undo) > n_hist)
    win.undo()
    pump()
    check("undo restores the blur", len(win.project.pages[0].blurs) == 1)
    win.project.pages[0].blurs.clear()

    # ------------------------------------------------------- multi-select done
    print("\nmark done")
    for p in win.project.pages:
        p.done = False
    win.queue.rebuild()
    win.queue.select(win.project.pages[0].uid)
    for u in [p.uid for p in win.project.pages[:3]]:
        win.queue.select(u, add=True)
    pump()
    check("three pages selected", len(win.queue.selected_uids()) == 3,
          str(len(win.queue.selected_uids())))
    win.done_and_next()
    pump()
    marked = [p.done for p in win.project.pages]
    check("every selected page is marked done", marked[:3] == [True, True, True],
          str(marked))
    check("unselected pages are left alone", marked[3] is False, str(marked))
    win.undo()
    pump()
    check("marking a batch done is undoable",
          not any(p.done for p in win.project.pages),
          str([p.done for p in win.project.pages]))

    # --------------------------------------------------- what crop-all reaches
    print("\ncrop all pages, and the modes that opt out")
    for p_ in win.project.pages:
        p_.crop_mode = "linked"
        p_.crop = [10.0, 20.0, 1000.0, 1400.0]
    src = win.project.pages[0]
    src.crop = [50.0, 60.0, 1800.0, 2600.0]

    win.project.pages[1].crop_mode = "own_position"
    win.project.pages[1].crop = [999.0, 888.0, 1200.0, 1500.0]
    locked = win.project.pages[3]
    locked.crop_mode = "locked"
    locked.crop = [7.0, 8.0, 1234.0, 5678.0]
    keep_locked = list(locked.crop)

    n = win.project.apply_crop_to_all(src)
    check("crop-all reaches every page it is allowed to", n == 2, str(n))
    check("an ordinary page takes the whole rectangle",
          [round(v) for v in win.project.pages[2].crop] == [50, 60, 1800, 2600],
          str(win.project.pages[2].crop))
    p1 = win.project.pages[1]
    check("an own-position page keeps where it sits",
          round(p1.crop[0]) == 999 and round(p1.crop[1]) == 888,
          str(p1.crop[:2]))
    check("but still takes the size, so the pages match",
          p1.output_size() == (1800, 2600), str(p1.output_size()))
    check("a locked page is not touched at all",
          list(locked.crop) == keep_locked, str(locked.crop))
    check("and a locked page cannot push its crop onto anyone",
          win.project.apply_crop_to_all(locked) == 0)

    # editing one page afterwards must not follow the others around
    win.queue.select(win.project.pages[2].uid)
    wait_until(lambda: win._proxy is not None)
    others = [tuple(p_.crop) for p_ in win.project.pages if p_ is not win._current]
    win.inspector.spins["x"].setValue(310)
    pump()
    check("a later edit stays on the page it was made on",
          [tuple(p_.crop) for p_ in win.project.pages
           if p_ is not win._current] == others, "other pages moved")

    locked.crop_mode = "linked"
    win.project.pages[1].crop_mode = "linked"
    win.project.apply_crop_to_all(src)

    # ------------------------------------------------------------ undo/redo
    print("\nundo / redo")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    before_angle = win._current.angle
    win.angle_strip.spin.setValue(-1.25)
    pump()
    win._end_history()
    check("angle changed", abs(win._current.angle - (-1.25)) < 1e-9)
    check("undo is now available", win.history.can_undo)
    win.undo()
    pump()
    check("undo restored the angle", abs(win._current.angle - before_angle) < 1e-9,
          f"{win._current.angle}")
    win.redo()
    pump()
    check("redo reapplied it", abs(win._current.angle - (-1.25)) < 1e-9,
          f"{win._current.angle}")

    n_before = len(win.project.pages)
    win.remove_uids([win.project.pages[3].uid])
    pump()
    check("page removed", len(win.project.pages) == n_before - 1)
    win.undo()
    pump()
    check("undo brings a removed page back", len(win.project.pages) == n_before)

    win.history.capture("Batch test")
    crops_before = [list(p.crop) for p in win.project.pages]
    for p in win.project.pages:
        p.crop = [0.0, 0.0, 500.0, 500.0]
    win.undo()
    pump()
    check("undo covers a whole-project change",
          [list(p.crop) for p in win.project.pages] == crops_before)
    check("current page survives undo as the same object",
          win._current is win.project.by_uid(win._current.uid))

    # ------------------------------------------------------------ reordering
    print("\nreordering and deletion")
    order_before = [p.name for p in win.project.pages]
    win.project.move([win.project.pages[2].uid], -1)
    check("move up swaps neighbours",
          [p.name for p in win.project.pages][1] == order_before[2],
          str([p.name for p in win.project.pages]))
    win.project.move([win.project.pages[1].uid], 1)
    check("move down undoes it", [p.name for p in win.project.pages] == order_before)

    check("cannot move the first page up",
          not win.project.move([win.project.pages[0].uid], -1))
    check("cannot move the last page down",
          not win.project.move([win.project.pages[-1].uid], 1))

    win.project.sort_pages("name", reverse=True)
    check("sort by name reversed",
          [p.name for p in win.project.pages] == sorted(order_before, reverse=True),
          str([p.name for p in win.project.pages]))
    win.project.sort_pages("name")
    check("sort by name", [p.name for p in win.project.pages] == sorted(order_before))

    uids = [p.uid for p in win.project.pages]
    win.project.reorder_visible(list(reversed(uids)))
    check("explicit reorder applied",
          [p.uid for p in win.project.pages] == list(reversed(uids)))
    win.project.reorder_visible(uids)

    win.queue.rebuild()
    pump()
    from scanforge.ui.queue_panel import _GAP_ROLE, _MARK_ROLE, _NUM_ROLE
    item0 = win.queue.list.item(0)
    check("the page's position is carried on the scan itself",
          item0.data(_NUM_ROLE) == 1, str(item0.data(_NUM_ROLE)))
    check("and the file name is not repeated under every one",
          item0.text() == "" and win.project.pages[0].name not in
          str(item0.data(_MARK_ROLE) or ""), repr(item0.data(_MARK_ROLE)))
    from scanforge.model import PageEdit as _Page
    check("an untouched page says nothing at all",
          not win.queue._label(_Page(path="/x/untouched.tif")),
          repr(win.queue._label(_Page(path="/x/untouched.tif"))))
    edited = win.project.pages[0]
    check("a page that has been worked on says what was done, in words",
          win.queue._label(edited) in ("edited", "locked")
          or "edited" in win.queue._label(edited),
          repr(win.queue._label(edited)))
    check("the file name is still there when you ask for it",
          win.project.pages[0].name in item0.toolTip(), item0.toolTip())

    # ------------------------------------------------- drag-to-reorder logic
    print("\ndrag reorder")
    q = win.queue
    names = lambda: [p.name for p in win.project.pages]          # noqa: E731
    uid = lambda i: win.project.pages[i].uid                     # noqa: E731

    def reorder(idxs, row):
        """Drop the pages at `idxs` onto insertion row `row`."""
        q.rebuild()
        picked = [uid(i) for i in idxs]
        q._apply_reorder(picked, row)
        return names()

    # `row` now counts only the pages that stayed put, because the dragged ones
    # are physically out of the list while you hold them
    base = names()
    check("drop one page after the first",
          reorder([0], 1) == [base[1], base[0], base[2], base[3]], str(names()))
    q.project.pages.sort(key=lambda p: p.name)

    check("drop first page at the end",
          reorder([0], 3) == base[1:] + [base[0]], str(names()))
    q.project.pages.sort(key=lambda p: p.name)

    check("drop last page at the start",
          reorder([3], 0) == [base[3]] + base[:3], str(names()))
    q.project.pages.sort(key=lambda p: p.name)

    check("drop two pages together at the end",
          reorder([0, 1], 2) == base[2:] + base[:2], str(names()))
    q.project.pages.sort(key=lambda p: p.name)

    check("dropping a page back where it was changes nothing",
          reorder([2], 2) == base, str(names()))

    check("non-adjacent multi-select keeps its own order",
          reorder([1, 3], 1) == [base[0], base[1], base[3], base[2]], str(names()))
    q.project.pages.sort(key=lambda p: p.name)

    # ---- the real gesture, driven with real mouse events end to end.
    # This is the test that was missing: the old one emitted reorderRequested
    # itself, so it proved the arithmetic while the actual drag never started.
    q.rebuild()
    pump(0.2)
    lst = q.list
    base = names()
    r0 = lst.visualItemRect(lst.item(0))
    if r0.width() <= 0:
        check("list laid out for geometry checks", False, "no item rects")
    else:
        lst.setCurrentRow(0)
        lst.item(0).setSelected(True)
        start = QPointF(r0.center())
        lst.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, start,
                                        Qt.LeftButton, Qt.LeftButton,
                                        Qt.NoModifier))
        check("a press alone lifts nothing", not lst.dragging)
        lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove,
                                       QPointF(start.x() + 40, start.y()),
                                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        check("dragging far enough lifts the page out", lst.dragging)
        gaps = [i for i in range(lst.count()) if lst.item(i).data(_GAP_ROLE)]
        check("and leaves a blank slot in its place", len(gaps) == 1, str(gaps))
        check("the list still holds every other page",
              lst.count() == len(base), f"{lst.count()} rows for {len(base)} pages")
        left = [lst.item(i).data(Qt.UserRole) for i in range(lst.count())
                if not lst.item(i).data(_GAP_ROLE)]
        check("the page being dragged is not among them",
              uid(0) not in left, str(left))

        # move it over the right-hand half of the third cell
        r2 = lst.visualItemRect(lst.item(2))
        over = QPointF(r2.right() - 2, r2.center().y())
        lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, over,
                                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        pump()
        moved_to = [i for i in range(lst.count()) if lst.item(i).data(_GAP_ROLE)][0]
        want = max(0, min(lst._slot_at(over), lst.count() - 1))
        check("the slot lands on the cell the cursor is in",
              moved_to == want, f"slot {moved_to}, cursor cell {want}")

        rec = []
        q.orderChanged.connect(lambda label: rec.append(label))
        lst.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, over,
                                          Qt.LeftButton, Qt.NoButton,
                                          Qt.NoModifier))
        pump()
        # the promise is "where the slot was, the page now is" - asserted from
        # where the slot actually sat rather than from a hardcoded order, so a
        # drift of one cell cannot hide inside the expected answer
        check("letting go puts the page exactly where the slot was",
              names().index(base[0]) == moved_to,
              f"page at {names().index(base[0])}, slot was {moved_to}")
        check("the drag is recorded for undo", rec == ["Reorder pages"], str(rec))
        check("and nothing is left lifted", not lst.dragging
              and not any(lst.item(i).data(_GAP_ROLE)
                          for i in range(lst.count())))
        win.undo()
        pump()
        check("undo restores the order", names() == base, str(names()))

        # ---- dropping a page back where it came from
        # Starting a drag physically removes the row from the widget, so every
        # way out of the drop has to rebuild - including the ones that change
        # nothing.  This one used to return early: the project kept all its
        # pages and the count stayed right, while the page itself vanished out
        # of the grid and the positions read 9, 10, 11, 13.  Picking a page up
        # and putting it straight back down is the most ordinary thing to do.
        for row_i in (0, 2, len(base) - 1):
            q.rebuild()
            pump(0.2)
            rows_before = lst.count()
            want = names()
            r = lst.visualItemRect(lst.item(row_i))
            if r.width() <= 0:
                continue
            here = QPointF(r.center())
            lst.setCurrentRow(row_i)
            lst.item(row_i).setSelected(True)
            lst.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, here,
                                            Qt.LeftButton, Qt.LeftButton,
                                            Qt.NoModifier))
            lst.mouseMoveEvent(QMouseEvent(
                QEvent.MouseMove, QPointF(here.x() + 40, here.y()),
                Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
            lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, here,
                                           Qt.NoButton, Qt.LeftButton,
                                           Qt.NoModifier))
            pump()
            lst.mouseReleaseEvent(QMouseEvent(
                QEvent.MouseButtonRelease, here, Qt.LeftButton,
                Qt.NoButton, Qt.NoModifier))
            pump()
            shown = {lst.item(i).data(Qt.UserRole) for i in range(lst.count())}
            missing = [p.name for p in win.project.pages if p.uid not in shown]
            check(f"a page dropped back on itself (row {row_i}) stays in the grid",
                  lst.count() == rows_before and not missing,
                  f"{lst.count()} rows of {rows_before}, missing {missing}")
            check(f"and the order is untouched (row {row_i})",
                  names() == want, str(names()))

        # ---- several pages at once, and the ghost that carries them
        q.rebuild()
        pump(0.2)
        base = names()
        lst.setCurrentRow(1)
        lst.clearSelection()
        for i in (0, 1, 2):
            lst.item(i).setSelected(True)
        check("three pages can be selected", len(lst.selectedItems()) == 3,
              str(len(lst.selectedItems())))
        rc = lst.visualItemRect(lst.item(1))
        lst.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress,
                                        QPointF(rc.center()), Qt.LeftButton,
                                        Qt.LeftButton, Qt.NoModifier))
        # Qt collapses a multi-selection on press unless its own drag machinery
        # is on; ours replaced it, so the collapse is deferred to release
        check("pressing one of them keeps the others selected",
              len(lst.selectedItems()) == 3, str(len(lst.selectedItems())))
        lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove,
                                       QPointF(rc.center().x() + 40, rc.center().y()),
                                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        check("all three lift together", lst.dragging
              and len(lst._drag_uids) == 3, str(len(lst._drag_uids)))
        check("a faded copy is picked up to carry around",
              lst._ghost is not None and not lst._ghost.isNull())
        check("and it follows the cursor",
              lst._ghost_rect().contains(QPointF(rc.center().x() + 40,
                                                 rc.center().y()).toPoint()))
        last = lst.visualItemRect(lst.item(lst.count() - 1))
        lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(last.center()),
                                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        lst.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease,
                                          QPointF(last.center()), Qt.LeftButton,
                                          Qt.NoButton, Qt.NoModifier))
        pump()
        check("they land together, in their own order",
              names() == base[3:] + base[:3], str(names()))
        check("and the ghost is put down", lst._ghost is None)
        win.undo()
        pump()

        # ---- the slot must sit on the cursor, coming from either direction
        q.rebuild()
        pump(0.2)
        lst.clearSelection()
        lst.item(0).setSelected(True)
        r0 = lst.visualItemRect(lst.item(0))
        lst.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(r0.center()),
                                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove,
                                       QPointF(r0.center().x() + 40, r0.center().y()),
                                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier))

        def slot_now():
            found = [i for i in range(lst.count()) if lst.item(i).data(_GAP_ROLE)]
            return found[0] if found else None

        wrong = []
        sweep = list(range(lst.count())) + list(range(lst.count() - 1, -1, -1))
        for i in sweep:
            rr = lst.visualItemRect(lst.item(i))
            if rr.width() <= 0:
                continue
            lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(rr.center()),
                                           Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
            want = max(0, min(lst._slot_at(rr.center()), lst.count() - 1))
            if slot_now() != want:
                wrong.append((i, want, slot_now()))
        # exactly the cell under the cursor - not "or the one before it", which
        # is how a one-cell lag went unnoticed the first time round
        check("the slot sits on the cursor's own cell, sweeping both ways",
              not wrong, str(wrong[:4]))

        seen_at = []
        mid = lst.visualItemRect(lst.item(min(2, lst.count() - 1))).center()
        far = lst.visualItemRect(lst.item(lst.count() - 1)).center()
        for _ in range(5):
            for pt in (far, mid):
                lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(pt),
                                               Qt.NoButton, Qt.LeftButton,
                                               Qt.NoModifier))
            seen_at.append(slot_now())
        check("and returning to a point always gives the same answer",
              len(set(seen_at)) == 1, str(seen_at))
        lst.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        pump()

        # Escape puts everything back
        q.rebuild()
        pump(0.15)
        r0 = lst.visualItemRect(lst.item(0))
        lst.setCurrentRow(0)
        lst.item(0).setSelected(True)
        lst.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(r0.center()),
                                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        lst.mouseMoveEvent(QMouseEvent(QEvent.MouseMove,
                                       QPointF(r0.center().x() + 40, r0.center().y()),
                                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        check("a drag can be started again", lst.dragging)
        lst.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        pump()
        check("Escape abandons it", not lst.dragging)
        check("and the page comes back", names() == base, str(names()))
        check("with the list whole again", lst.count() == len(base),
              str(lst.count()))

    # ------------------------------------------------------ column snapping
    print("\ncolumn snapping")
    col = q.column_width()
    check("a column has a real width", col > 40, str(col))
    for extra in (0, 3, 12):
        w = q.width_for_columns(2) + extra
        check(f"{extra} px past two columns snaps back to two",
              q.snapped_width(w) == q.width_for_columns(2),
              f"{q.snapped_width(w)} vs {q.width_for_columns(2)}")
    check("most of the way to three columns rounds up",
          q.snapped_width(q.width_for_columns(2) + int(col * 0.7))
          == q.width_for_columns(3),
          str(q.snapped_width(q.width_for_columns(2) + int(col * 0.7))))
    check("it never collapses below one column",
          q.snapped_width(4) == q.width_for_columns(1),
          str(q.snapped_width(4)))
    # the grid is the right-hand pane now, so the divider that sizes it is the
    # one between the editing area and the column
    sizes = win.splitter.sizes()
    win.splitter.setSizes([sizes[0] - 17, q.width_for_columns(2) + 17])
    win._snap_queue_width()
    pump()
    check("dragging the divider lands it on a column boundary",
          win.splitter.sizes()[1] == q.width_for_columns(2),
          f"{win.splitter.sizes()[1]} vs {q.width_for_columns(2)}")

    # ------------------------------------------------- move to position x of n
    print("\nmove to position")
    q.project.pages.sort(key=lambda p: p.name)
    base = names()
    n = len(base)
    check("move last to position 1",
          win.project.move_to([uid(n - 1)], 1) and names() == [base[3]] + base[:3],
          str(names()))
    q.project.pages.sort(key=lambda p: p.name)
    check("move first to position n",
          win.project.move_to([uid(0)], n) and names() == base[1:] + [base[0]],
          str(names()))
    q.project.pages.sort(key=lambda p: p.name)
    check("move two pages to position 3",
          win.project.move_to([uid(0), uid(1)], 3)
          and names() == [base[2], base[3], base[0], base[1]], str(names()))
    q.project.pages.sort(key=lambda p: p.name)
    check("position beyond the end clamps to the end",
          win.project.move_to([uid(0)], 999) and names()[-1] == base[0], str(names()))
    q.project.pages.sort(key=lambda p: p.name)
    check("position below 1 clamps to the start",
          win.project.move_to([uid(2)], 0) and names()[0] == base[2], str(names()))
    q.project.pages.sort(key=lambda p: p.name)
    check("moving to its current position changes nothing",
          win.project.move_to([uid(1)], 2) and names() == base, str(names()))
    check("Ctrl+M is bound", win.a_move_to.shortcut().toString() == "Ctrl+M",
          win.a_move_to.shortcut().toString())

    from scanforge.ui.queue_panel import MoveToDialog
    from PySide6.QtWidgets import QDialogButtonBox
    dlg = MoveToDialog(count=40, current=12, what="a page", parent=win)
    ok_btn = dlg.buttons.button(QDialogButtonBox.Ok)
    check("a valid position is accepted", ok_btn.isEnabled())
    dlg.edit.setText("250")
    check("an out-of-range number cannot be confirmed", not ok_btn.isEnabled())
    check("and says why, in red", "only 40" in dlg.note.text()
          and "ff7b72" in dlg.note.styleSheet(), dlg.note.text())
    dlg.edit.setText("0")
    check("zero is refused too", not ok_btn.isEnabled(), dlg.note.text())
    dlg.edit.setText("abc")
    check("text is refused", not ok_btn.isEnabled(), dlg.note.text())
    dlg.edit.setText("")
    check("empty is refused", not ok_btn.isEnabled(), dlg.note.text())
    dlg.edit.setText("40")
    check("the last position is allowed", ok_btn.isEnabled())
    check("and the typed value is what comes back", dlg.value() == 40)
    dlg.close()

    # ---------------------------------------------------------------- naming
    print("\nnaming")
    cfg = ExportSettings(out_dir=os.path.join(TMP, "out"), naming="{doc}-{page}",
                         digits=4, start_number=1, make_pdf=True,
                         document="Sony_KV-16G1")
    check("dash naming is the default",
          ExportSettings().naming == "{doc}-{page}", ExportSettings().naming)
    check("no TIFF compression by default",
          ExportSettings().compression == "None", ExportSettings().compression)
    check("subfolder off by default", not ExportSettings().subfolder_per_doc)
    check("pdf dpi follows the scans",
          resolve_pdf_dpi(ExportSettings(), win.project.pages) == 600.0,
          str(resolve_pdf_dpi(ExportSettings(), win.project.pages)))
    check("an explicit pdf dpi still wins",
          resolve_pdf_dpi(ExportSettings(pdf_dpi=150), win.project.pages) == 150.0)
    check("first page is 0001",
          format_name(cfg, "Sony_KV-16G1", "scan_07", 1) == "Sony_KV-16G1-0001",
          format_name(cfg, "Sony_KV-16G1", "scan_07", 1))
    check("tenth page is 0010",
          format_name(cfg, "Sony_KV-16G1", "scan_16", 10) == "Sony_KV-16G1-0010")
    check("target file name",
          os.path.basename(output_path(win.project.pages[0], cfg, 1)) ==
          "Sony_KV-16G1-0001.tif",
          os.path.basename(output_path(win.project.pages[0], cfg, 1)))
    check("no subfolder by default",
          os.path.dirname(output_path(win.project.pages[0], cfg, 1)) == cfg.out_dir,
          os.path.dirname(output_path(win.project.pages[0], cfg, 1)))
    check("pdf drops the page number",
          os.path.basename(pdf_path(cfg, "Sony_KV-16G1")) == "Sony_KV-16G1.pdf",
          os.path.basename(pdf_path(cfg, "Sony_KV-16G1")))
    cfg2 = ExportSettings(out_dir="x", naming="{doc}_{page}", digits=3,
                          start_number=5, document="Doc")
    check("digits and start number both apply",
          format_name(cfg2, "Doc", "s", 1) == "Doc_005",
          format_name(cfg2, "Doc", "s", 1))

    dlg2 = ExportDialog(list(win.project.pages), cfg, win)
    check("dialog carries the document name",
          dlg2.ed_doc.text() == "Sony_KV-16G1", dlg2.ed_doc.text())
    check("pdf resolution prefilled from the scans",
          dlg2.sp_pdf_dpi.value() == 600.0, str(dlg2.sp_pdf_dpi.value()))
    dlg2.cmb_preset.setCurrentText("Document name + page number (dash)")
    got = dlg2.settings()
    check("preset drives the pattern", got.naming == "{doc}-{page}", got.naming)
    check("dialog previews real file names",
          "Sony_KV-16G1-0001.tif" in dlg2.lbl_preview.text(),
          dlg2.lbl_preview.text().replace("\n", " | ")[:120])
    check("dialog previews the pdf",
          "Sony_KV-16G1.pdf" in dlg2.lbl_preview.text())
    check("dialog confirms one uniform size",
          "Every page" in dlg2.lbl_preview.text())

    # the page files can be a means to an end: off by default, and forced
    # back on when nothing else is being produced from them
    check("the export omits the loose page files by default",
          not dlg2.chk_keep.isChecked() and dlg2.chk_keep.isEnabled())
    dlg2.chk_zip.setChecked(True)
    dlg2.chk_pdf.setChecked(False)
    dlg2.chk_keep.setChecked(False)
    check("so a zip-only run does not keep them",
          not dlg2.settings().keep_images)
    check("and the preview says the pages will be discarded",
          "discarded" in dlg2.lbl_preview.text(), dlg2.lbl_preview.text()[:60])
    dlg2.chk_zip.setChecked(False)
    dlg2.chk_pdf.setChecked(False)
    pump()
    check("with neither a zip nor a PDF the pages are kept regardless",
          dlg2.settings().keep_images)
    check("and the choice is greyed out with a reason",
          not dlg2.chk_keep.isEnabled()
          and "nothing else" in dlg2.chk_keep.text(), dlg2.chk_keep.text())
    dlg2.chk_zip.setChecked(True)
    dlg2.close()

    # ---------------------------------------------------------------- spread
    print("\nspread alignment")
    win.queue.list.clearSelection()
    win.queue.select(win.project.pages[2].uid)
    pump()
    win.open_spread()
    check("one selection starts a pick, not an error",
          win._spread_pick == win.project.pages[2].uid, str(win._spread_pick))
    check("status bar asks for the other page",
          "Click the page to pair with" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())
    win.escape()
    check("Esc cancels the pick", win._spread_pick is None)

    win.open_spread()
    left = win.project.pages[2]
    right = win.project.pages[3]
    win.show_page(right.uid)          # what clicking the queue does
    # both proxies load on a worker, so the aligner appears a moment later
    check("clicking the facing page opens the aligner",
          wait_until(lambda: win._mode is not None, 30), "no aligner appeared")
    check("and it takes the canvas's place rather than a new window",
          win.center.currentWidget() is win._mode
          and win.center.currentWidget() is not win.canvas,
          str(type(win.center.currentWidget()).__name__))
    check("the page grid is still there",
          win.queue.isVisible())
    # The options bar describes the page on the canvas.  With the canvas
    # hidden, it goes too - it used to stay, and its angle slider went on
    # turning the page nobody could see.
    check("the options bar leaves with the canvas",
          not win.options_bar.isVisible() and not win.options_bar.isEnabled())
    dlgs = win.findChildren(SpreadPane)
    if dlgs:
        dlg = win._mode
        v = dlg.view
        lr, rr = v.crop_spread("left"), v.crop_spread("right")
        check("pages sit side by side at the gutter",
              abs(rr.left() - lr.right()) < 1e-6,
              f"left ends {lr.right():.1f}, right starts {rr.left():.1f}")
        check("spread view paints both pages", nonblank(paint(v)))
        v.add_guide(True)
        check("spread view paints with guides", nonblank(paint(v)))

        # ---- the editing canvas is out of play while the aligner has the room
        # The canvas is still in the stack behind the aligner, holding a page.
        # Every shortcut used to reach it: Space put guides on the hidden page,
        # and Shift+C, Ctrl+D, E and the rotate keys were live on it too.
        n_before = len(win.canvas.guides_h) + len(win.canvas.guides_v)
        spread_guides = len(v.guides) + len(v.vguides)
        for act in (win.a_guide_h, win.a_guide_v, win.a_tool_crop_all,
                    win.a_clear_crop, win.a_fitpage, win.a_rot_cw, win.a_undo):
            act.trigger()
        pump()
        check("shortcuts do not reach the page behind the aligner",
              len(win.canvas.guides_h) + len(win.canvas.guides_v) == n_before,
              f"{len(win.canvas.guides_h) + len(win.canvas.guides_v)} guides, "
              f"was {n_before}")
        check("and the editing actions are visibly off",
              not win.a_guide_h.isEnabled() and not win.tool_column.isEnabled(),
              f"guide={win.a_guide_h.isEnabled()}, "
              f"column={win.tool_column.isEnabled()}")

        # Space belongs to the aligner while it is open
        v.setFocus()
        v.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier))
        v.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Space,
                                  Qt.ShiftModifier))
        pump()
        check("Space drops guides on the spread instead",
              len(v.guides) + len(v.vguides) == spread_guides + 2,
              f"{len(v.guides)} level, {len(v.vguides)} upright")

        # ---- the control strip has to fit, at any width
        # It used to be one long row in a scroll area, so in a narrow window
        # Revert / Join / Keep alignment - the controls that finish the job -
        # sat off the right-hand end behind a scrollbar.
        from PySide6.QtWidgets import QScrollArea
        bar = dlg.findChild(QWidget, "spread_controls")
        check("the aligner's controls are a real strip, not a scrolling one",
              bar is not None and not dlg.findChildren(QScrollArea),
              f"bar={bar is not None}, "
              f"{len(dlg.findChildren(QScrollArea))} scroll areas")
        if bar is not None:
            check("the strip does not demand more width than a page thumbnail",
                  bar.minimumSizeHint().width() < 420,
                  f"{bar.minimumSizeHint().width()} px")
            for width in (1400, 1000, 760, 560, 420):
                dlg.resize(width, 700)
                pump(0.1)
                spill = [c.objectName() or type(c).__name__
                         for c in bar.findChildren(QWidget)
                         if c.parent() is bar
                         and (c.geometry().right() > bar.width()
                              or c.geometry().bottom() > bar.height())]
                # `sizeHint().height()` is no use here: for a wrapping layout
                # Qt measures it at the layout's *minimum* width, which is the
                # tallest the strip ever gets. What matters is the height it
                # needs at the width it actually has.
                need = bar.layout().heightForWidth(bar.width())
                check(f"every control is on screen at {width} px wide",
                      not spill and bar.height() >= need,
                      f"{len(spill)} off the edge: {spill[:4]}; "
                      f"bar {bar.width()}x{bar.height()}, needs {need}")
            dlg.resize(1100, 700)
            pump(0.1)

        # the rest of the document is out of play while two pages are open
        flags = {i: bool(win.queue._items[p.uid].flags() & Qt.ItemIsEnabled)
                 for i, p in enumerate(win.project.pages)}
        pair = {win.project.index_of(dlg.left.uid),
                win.project.index_of(dlg.right.uid)}
        check("only the two pages being aligned stay selectable",
              {i for i, on in flags.items() if on} == pair,
              f"enabled {sorted(i for i, on in flags.items() if on)}, "
              f"aligning {sorted(pair)}")
        check("and the options bar cannot be used on a page that is not on screen",
              not win.options_bar.isEnabled())
        a_before = win._current.angle if win._current else None
        win.angle_strip.spin.setValue(1.5)
        pump()
        check("its angle control does not reach the hidden page",
              win._current is None or win._current.angle == a_before,
              f"{a_before} -> {win._current.angle if win._current else None}")
        check("the page grid keeps its columns in spread mode",
              win.queue.columns_for_width(win.queue.width()) >= 2,
              f"{win.queue.columns_for_width(win.queue.width())} columns at "
              f"{win.queue.width()} px")

        # a guide belongs in the middle of the window, not the middle of the
        # spread - they are only the same thing when the view is fitted
        v.clear_guides()

        # linked crop sizes: a bound spread has to come out as two pages of
        # the same shape, whatever the scanner did with each half
        keep_l_start = list(dlg.left.crop)
        keep_r_start = list(dlg.right.crop)
        dlg.left.crop = [10.0, 20.0, 1000.0, 1400.0]
        dlg.right.crop = [30.0, 40.0, 900.0, 1300.0]
        v.active = "left"
        check("the two halves start at different sizes",
              dlg.left.output_size() != dlg.right.output_size(),
              f"{dlg.left.output_size()} vs {dlg.right.output_size()}")
        pane_link = getattr(dlg, "chk_link_size", None)
        check("the aligner offers a size link", pane_link is not None)

        pane_link.setChecked(True)
        pump()
        check("switching it on matches them there and then",
              dlg.left.output_size() == dlg.right.output_size(),
              f"{dlg.left.output_size()} vs {dlg.right.output_size()}")
        check("and it took the size from the active page, leaving the other "
              "page where it sits",
              dlg.right.crop[0] == 30.0 and dlg.right.crop[1] == 40.0,
              str(dlg.right.crop[:2]))

        # drag a crop frame by a handle: the other page follows in size only
        v.active = "right"
        r = v.crop_spread("right")
        grab = QPointF(r.center().x(), r.bottom())
        side, handle, idx = v._hit_handle(grab)
        check("a crop frame can be grabbed by its handles",
              side == "right" and handle == "b", f"{side} {handle}")
        before_pos = list(dlg.left.crop[:2])
        scr = v._to_screen(grab)
        v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, scr,
                                      Qt.LeftButton, Qt.LeftButton,
                                      Qt.NoModifier))
        end = QPointF(scr.x(), scr.y() - 60)
        v.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, end, Qt.NoButton,
                                     Qt.LeftButton, Qt.NoModifier))
        v.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, end,
                                        Qt.LeftButton, Qt.NoButton,
                                        Qt.NoModifier))
        pump()
        check("resizing one page resizes the other with it",
              dlg.left.output_size() == dlg.right.output_size(),
              f"{dlg.left.output_size()} vs {dlg.right.output_size()}")
        check("the drag actually made the pages shorter",
              dlg.right.crop[3] < 1400.0, str(dlg.right.crop[3]))
        check("and the linked page did not move, only change shape",
              list(dlg.left.crop[:2]) == before_pos,
              f"{dlg.left.crop[:2]} vs {before_pos}")

        # with the link off the two are independent again
        pane_link.setChecked(False)
        pump()
        v.active = "left"
        lr2 = v.crop_spread("left")
        grab = QPointF(lr2.right(), lr2.center().y())
        scr = v._to_screen(grab)
        keep_right = dlg.right.output_size()
        v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, scr,
                                      Qt.LeftButton, Qt.LeftButton,
                                      Qt.NoModifier))
        end = QPointF(scr.x() - 40, scr.y())
        v.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, end, Qt.NoButton,
                                     Qt.LeftButton, Qt.NoModifier))
        v.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, end,
                                        Qt.LeftButton, Qt.NoButton,
                                        Qt.NoModifier))
        pump()
        check("unlinked, one page resizes on its own",
              dlg.left.output_size() != dlg.right.output_size()
              and dlg.right.output_size() == keep_right,
              f"{dlg.left.output_size()} vs {dlg.right.output_size()}")

        # a crop cannot be dragged away to nothing
        v.active = "left"
        lr3 = v.crop_spread("left")
        v._resize_crop("left", "r", list(dlg.left.crop), -1e6, 0.0)
        check("a page cannot be shrunk to nothing", dlg.left.crop[2] >= 40.0,
              str(dlg.left.crop[2]))

        dlg.left.crop = list(keep_l_start)
        dlg.right.crop = list(keep_r_start)
        v.active = "left"
        v.add_guide(True)
        mid = v._to_spread(QPointF(v.width() / 2.0, v.height() / 2.0))
        check("a spread guide lands on the middle of the viewport",
              abs(v.guides[0] - mid.y()) < 1.0,
              f"{v.guides[0]:.0f} vs {mid.y():.0f}")
        v.clear_guides()
        v.match_size("both")
        check("match size equalises output",
              dlg.left.output_size() == dlg.right.output_size(),
              f"{dlg.left.output_size()} vs {dlg.right.output_size()}")
        dlg._accept()
        pump()
        check("spread pairing recorded",
              dlg.left.spread_partner == dlg.right.uid)
        check("both spread pages are locked against the shared crop",
              dlg.left.crop_mode == "locked" and dlg.right.crop_mode == "locked",
              f"{dlg.left.crop_mode}, {dlg.right.crop_mode}")
        keep_l, keep_r = list(dlg.left.crop), list(dlg.right.crop)
        other_page = next(p for p in win.project.pages
                          if p.uid not in (dlg.left.uid, dlg.right.uid))
        other_page.crop = [1.0, 2.0, 900.0, 900.0]
        win.project.apply_crop_to_all(other_page)
        check("and the shared crop no longer destroys the alignment",
              dlg.left.crop == keep_l and dlg.right.crop == keep_r,
              f"{dlg.left.crop} / {dlg.right.crop}")
        check("pages stayed separate files", dlg.left.path != dlg.right.path)
        check("finishing puts the canvas back",
              win._mode is None and win.center.currentWidget() is win.canvas)

    # ------------------------------------------------- the first crop
    print("\ncropping from the sheet edge")
    # this section deliberately empties the crops, so put back exactly what
    # every other section left behind rather than a plausible-looking blank
    saved_state = [(p_.uid, list(p_.crop) if p_.crop else None, p_.crop_mode)
                   for p_ in win.project.pages]
    saved_link = None
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.canvas.fit()
    pump()
    c = win.canvas
    page = win._current
    win.clear_crop()
    pump()
    check("a fresh page has no crop of its own", page.crop is None,
          str(page.crop))
    full = c.crop_scene()
    check("the implied frame is still the whole sheet, for geometry",
          full.width() > 1 and full.height() > 1,
          f"{full.width():.0f}x{full.height():.0f}")

    # ...but nothing is drawn and nothing is grabbable.  A frame around the
    # whole sheet is indistinguishable from a full-page crop somebody chose,
    # and invisible handles meant a click near the edge silently created one.
    edge = QPointF(full.left(), full.center().y())
    check("an uncropped page offers no handles to grab",
          c._hit(edge)[0] is None, str(c._hit(edge)[0]))
    check("and nothing in the middle either",
          c._hit(full.center())[0] is None, str(c._hit(full.center())[0]))

    # the crop tool is how a page gets its first crop; then the handles appear
    win.set_tool(Tool.CROP)
    c.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(120, 120),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    c.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease,
                                    QPointF(121, 121),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    check("clicking with the crop tool gives the page a crop",
          page.crop is not None, str(page.crop))
    check("and now the edge can be grabbed",
          c._hit(QPointF(c.crop_scene().left(),
                         c.crop_scene().center().y()))[0] == "l",
          str(c._hit(QPointF(c.crop_scene().left(),
                             c.crop_scene().center().y()))[0]))

    win.set_tool(Tool.CURSOR)
    start = c.mapFromScene(edge)
    c.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(start),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    c.mouseMoveEvent(QMouseEvent(QEvent.MouseMove,
                                 QPointF(start.x() + 60, start.y()),
                                 Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    c.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease,
                                    QPointF(start.x() + 60, start.y()),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    check("dragging the edge crops the page", page.crop is not None,
          str(page.crop))
    if page.crop:
        check("it took the sheet in from the left, keeping the rest",
              page.crop[0] > 1 and page.crop[3] > full.height() * 0.5,
              f"x={page.crop[0]:.0f} h={page.crop[3]:.0f}")
    check("and it stayed on that page, as crop-page always does",
          sum(1 for p_ in win.project.pages if p_.crop) >= 1)

    for uid, crop, mode in saved_state:
        p_ = win.project.by_uid(uid)
        if p_ is not None:
            p_.crop = list(crop) if crop else None
            p_.crop_mode = mode
    win.queue.rebuild()
    pump()
    check("the section put the project back as it found it",
          [(p_.uid, list(p_.crop) if p_.crop else None, p_.crop_mode)
           for p_ in win.project.pages] == saved_state
)

    # ------------------------------------------------------- the angle strip
    print("\nthe angle strip")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    pump()
    st = win.angle_strip
    check("straightening left the side panel",
          not hasattr(win.inspector, "spin_angle")
          and not hasattr(win.inspector, "btn_ccw"))
    check("the strip has the three controls and nothing else",
          hasattr(st, "spin") and hasattr(st, "slider") and hasattr(st, "btn_zero"))

    st.spin.setValue(0.75)
    pump()
    check("typing an angle turns the page",
          abs(win._current.angle - 0.75) < 1e-6, f"{win._current.angle:+.3f}")
    check("and the slider went with it", st.slider.value() == 750,
          str(st.slider.value()))
    st.slider.setValue(-1200)
    pump()
    check("dragging the slider turns it too",
          abs(win._current.angle + 1.2) < 1e-6, f"{win._current.angle:+.3f}")
    check("and the box went with it", abs(st.spin.value() + 1.2) < 1e-6,
          str(st.spin.value()))
    st.btn_zero.click()
    pump()
    check("and zero puts it back square", abs(win._current.angle) < 1e-9,
          f"{win._current.angle:+.3f}")

    # an angle set from anywhere else has to reach the strip
    win.canvas.set_angle(0.33)
    win.angle_strip.refresh()
    pump()
    check("an angle set elsewhere shows in the strip",
          abs(st.spin.value() - 0.33) < 1e-6, str(st.spin.value()))
    win.canvas.set_angle(0.0)
    win.angle_strip.refresh()
    pump()

    # ------------------------------------------------------ orientation tools
    print("\norientation in the column")
    for a in (win.a_rot_ccw, win.a_rot_cw, win.a_flip_h, win.a_flip_v):
        check(f"{a.text().replace('&', '')} is in the tool column",
              a in win.tool_column.actions())
    q0 = win._current.quadrant
    win.a_rot_cw.trigger()
    pump()
    check("rotate right turns the page a quarter",
          win._current.quadrant == (q0 + 1) % 4, str(win._current.quadrant))
    win.a_rot_ccw.trigger()
    pump()
    check("and rotate left turns it back", win._current.quadrant == q0,
          str(win._current.quadrant))
    f0 = win._current.flip_h
    win.a_flip_h.trigger()
    pump()
    check("flip horizontally mirrors the page", win._current.flip_h != f0)
    win.a_flip_h.trigger()
    pump()
    check("and flipping again puts it back", win._current.flip_h == f0)

    # ---------------------------------------------------------- guides
    print("\nguides")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.canvas.fit()
    pump()
    c = win.canvas
    c.clear_guides()
    check("the straightedge is gone for good",
          not hasattr(c, "align_line") and not hasattr(Tool, "ALIGN"))
    check("guides are on Space and Shift+Space",
          win.a_guide_h.shortcut().toString() == "Space"
          and win.a_guide_v.shortcut().toString() == "Shift+Space",
          f"{win.a_guide_h.shortcut().toString()!r} / "
          f"{win.a_guide_v.shortcut().toString()!r}")
    check("and they have their own segment in the top bar",
          all(a in win.toolbar_main.actions()
              for a in (win.a_guide_h, win.a_guide_v, win.a_guide_clear)))
    check("Space no longer holds the canvas in pan mode",
          not hasattr(c, "_space"))

    # a guide lands in the middle of what you are looking at, not the middle
    # of the page - the two are different as soon as you zoom in
    c.fit()
    pump()
    win._add_guide(True)
    win._add_guide(False)
    centre = c.mapToScene(c.viewport().rect().center())
    check("a horizontal guide lands on the view's centre line",
          abs(c.guides_h[0] - centre.y()) < 1.5,
          f"{c.guides_h[0]:.0f} vs {centre.y():.0f}")
    check("and a vertical one likewise",
          abs(c.guides_v[0] - centre.x()) < 1.5,
          f"{c.guides_v[0]:.0f} vs {centre.x():.0f}")

    c.clear_guides()
    c.zoom_to(2.5) if hasattr(c, "zoom_to") else c.scale(3.0, 3.0)
    c.centerOn(centre.x() + 400, centre.y() + 600)
    pump()
    win._add_guide(True)
    moved = c.mapToScene(c.viewport().rect().center())
    check("zoomed in, the guide follows the view rather than the page",
          abs(c.guides_h[0] - moved.y()) < 2.0,
          f"guide {c.guides_h[0]:.0f}, view centre {moved.y():.0f}, "
          f"page centre {centre.y():.0f}")

    win._add_guide(True)
    check("a second guide does not hide under the first",
          abs(c.guides_h[1] - c.guides_h[0]) > 1e-6,
          f"{c.guides_h[0]:.1f} vs {c.guides_h[1]:.1f}")

    win._clear_guides()
    check("clear takes them all off", not c.guides_h and not c.guides_v)

    c.fit()
    pump()

    # --------------------------------------------------------- dropping files
    print("\ndropping a folder anywhere")
    from PySide6.QtCore import QMimeData, QUrl
    from PySide6.QtGui import QDropEvent

    def drop_on(widget, paths, pos=None):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        pt = QPointF(pos or widget.rect().center())
        ev = QDropEvent(pt, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        widget.dropEvent(ev)
        pump()
        return ev.isAccepted()

    seen = []
    real_take = win.take_dropped_paths
    win.take_dropped_paths = lambda ps: seen.append(list(ps))
    win.canvas.filesDropped.disconnect()
    win.canvas.filesDropped.connect(win.take_dropped_paths)

    check("the canvas accepts drops at all", win.canvas.acceptDrops())
    accepted = drop_on(win.canvas, ["/tmp/dropped_folder"])
    check("dropping on the page reaches the window",
          accepted and seen == [["/tmp/dropped_folder"]], str(seen))

    seen.clear()
    drop_on(win, ["/tmp/another"])
    check("and so does dropping on the window chrome",
          seen == [["/tmp/another"]], str(seen))

    seen.clear()
    drop_on(win.queue.list, ["/tmp/third"])
    check("the page grid still takes them, by the same route",
          seen == [["/tmp/third"]], str(seen))

    win.canvas.filesDropped.disconnect()
    win.take_dropped_paths = real_take
    win.canvas.filesDropped.connect(win.take_dropped_paths)

    # a project file opens rather than being added as if it were a scan
    proj_path = os.path.join(TMP, "dropped.sfproj")
    win.project.save(proj_path)
    opened = []
    real_open = win._open_project_file
    win._open_project_file = lambda p_: opened.append(p_)
    win.take_dropped_paths([proj_path, "/tmp/a_scan.tif"])
    check("a dropped project opens instead of being imported",
          opened == [proj_path], str(opened))
    win._open_project_file = real_open

    # ---- recent projects
    saved_path = win.project.path
    rp = os.path.join(TMP, "recent test.sfproj")
    win.project.path = rp
    win.save_project()
    check("saving a project puts it on the recent list",
          win._recent_projects()[:1] == [os.path.abspath(rp)],
          str(win._recent_projects()))
    win._fill_recent_menu()
    titles = [a.text() for a in win.m_recent.actions()]
    check("File > Open recent lists it", "recent test.sfproj" in titles,
          str(titles))
    win.welcome.set_recent(win._recent_projects())
    from PySide6.QtWidgets import QPushButton as _QPB
    names = [b.text() for b in win.welcome.recent_box.findChildren(_QPB)]
    check("and so does the welcome screen", "recent test" in names, str(names))
    win.project.path = saved_path

    # ------------------------------------------------------------- zooming
    print("\nzooming")
    c = win.canvas
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    c.fit()
    pump()

    check("100% and 50% are gone", not hasattr(win, "a_100")
          and not hasattr(win, "a_50"))

    # zoom means nothing with no page open, and a transform applied to an empty
    # scene used to stick: the first page then arrived at that magnification
    keep_page, keep_proxy = win._current, win._proxy
    win.canvas.set_page(None, None)
    pump()
    check("with no page, the zoom controls are switched off",
          not win.zoom_slider.isEnabled() and not win.btn_fit.isEnabled()
          and not win.a_fit.isEnabled() and not win.a_zoomin.isEnabled())
    check("and the readout says so, rather than a number",
          "%" not in win.lbl_zoom.text(), win.lbl_zoom.text())
    before = win.canvas.transform().m11()
    win.canvas.zoom_by(3.0)
    win.canvas.zoom_to(1.0)
    win.canvas.fit()
    check("and zooming an empty canvas does nothing at all",
          abs(win.canvas.transform().m11() - before) < 1e-9,
          f"{before} -> {win.canvas.transform().m11()}")
    win.canvas.set_page(keep_page, keep_proxy)
    pump()
    check("with a page open they come back",
          win.zoom_slider.isEnabled() and win.a_fit.isEnabled())
    check("and so is show thirds", not hasattr(win, "a_thirds")
          and not hasattr(c, "show_thirds"))

    # the slider is the zoom control, and it runs both ways
    fitted = c.zoom_percent()
    check("the status bar reports the fitted zoom", 1 < fitted < 100,
          f"{fitted:.1f}%")
    check("the slider followed the canvas",
          abs(win.zoom_slider.value() - win._zoom_to_slider(fitted)) <= 1,
          f"{win.zoom_slider.value()} vs {win._zoom_to_slider(fitted)}")
    check("and the readout agrees", "%" in win.lbl_zoom.text(),
          win.lbl_zoom.text())

    win.zoom_slider.setValue(win._zoom_to_slider(fitted * 3))
    pump()
    check("dragging the slider zooms the canvas",
          abs(c.zoom_percent() - fitted * 3) < fitted * 0.15,
          f"{c.zoom_percent():.1f}% wanted {fitted * 3:.1f}%")

    c.fit()
    pump()
    check("and Fit puts the slider back",
          abs(win.zoom_slider.value() - win._zoom_to_slider(c.zoom_percent())) <= 1,
          str(win.zoom_slider.value()))
    check("the fit button in the status bar is the Fit action",
          win.btn_fit.defaultAction() is win.a_fit)

    # the slider is logarithmic, so the low end is not all crammed together
    lo = win._slider_to_zoom(0)
    mid = win._slider_to_zoom(win.ZOOM_STEPS // 2)
    hi = win._slider_to_zoom(win.ZOOM_STEPS)
    check("the slider is logarithmic, not linear",
          abs(mid - (lo * hi) ** 0.5) < 0.5,
          f"{lo:.1f}% · {mid:.1f}% · {hi:.0f}%")

    # the wheel: bare scrolls, shift scrolls across, ctrl zooms
    from PySide6.QtGui import QWheelEvent
    def wheel(mods, dy=120):
        pos = QPointF(c.viewport().rect().center())
        ev = QWheelEvent(pos, c.viewport().mapToGlobal(pos.toPoint()),
                         QPoint(0, 0), QPoint(0, dy), Qt.NoButton, mods,
                         Qt.NoScrollPhase, False)
        c.wheelEvent(ev)
        pump()

    c.fit()
    pump()
    c.zoom_by(4.0)          # zoom in so there is somewhere to scroll to
    pump()
    vbar, hbar = c.verticalScrollBar(), c.horizontalScrollBar()
    v0, h0, z0 = vbar.value(), hbar.value(), c.zoom_percent()
    wheel(Qt.NoModifier)
    check("a bare wheel scrolls down the page, it does not zoom",
          vbar.value() != v0 and abs(c.zoom_percent() - z0) < 1e-6,
          f"scroll {v0} -> {vbar.value()}, zoom {z0:.1f}%")

    v1, h1 = vbar.value(), hbar.value()
    wheel(Qt.ShiftModifier)
    check("Shift+wheel scrolls across instead",
          hbar.value() != h1 and vbar.value() == v1,
          f"h {h1} -> {hbar.value()}, v {v1} -> {vbar.value()}")

    z1 = c.zoom_percent()
    wheel(Qt.ControlModifier)
    check("Ctrl+wheel is what zooms", c.zoom_percent() > z1,
          f"{z1:.1f}% -> {c.zoom_percent():.1f}%")

    keys = [k.toString() for k in win.a_zoomin.shortcuts()]
    check("zoom in is bound to both the keycap and what the key sends",
          "Ctrl++" in keys and "Ctrl+=" in keys, str(keys))
    check("and the menu shows the keycap", keys[0] == "Ctrl++", str(keys))
    check("zoom out likewise",
          [k.toString() for k in win.a_zoomout.shortcuts()] == ["Ctrl+-"],
          str([k.toString() for k in win.a_zoomout.shortcuts()]))
    c.fit()
    pump()

    # ------------------------------------------------------ grid and chrome
    print("\ngrid and chrome")
    c = win.canvas
    c.clear_guides()
    win.toggle_grid(False)
    pump()
    plain = paint(c)
    win.toggle_grid(True)
    pump()
    gridded = paint(c)

    def differing(a, b):
        """Total ink laid down, not merely how many pixels moved.

        Counting changed pixels barely separates a hairline from a two-pixel
        line - it is the strength of the change that decides whether the eye
        finds the grid.
        """
        total = 0
        for y in range(0, a.height(), 3):
            for x in range(0, a.width(), 3):
                pa, pb = a.pixel(x, y), b.pixel(x, y)
                if pa != pb:
                    for shift in (0, 8, 16):
                        total += abs(((pa >> shift) & 255) - ((pb >> shift) & 255))
        return total

    changed = differing(plain, gridded)
    check("the grid is drawn at all", changed > 0, f"ink {changed}")

    # and it is drawn heavily enough to see: the old hairline at alpha 55 is
    # the thing being measured against, not an arbitrary threshold.  Each
    # comparison re-takes its own grid-off shot, so nothing but the pen differs
    # between the two - a stale one would carry any layout change into both
    # numbers and flatten the ratio.
    import scanforge.ui.canvas as canvas_mod
    now = canvas_mod.GRID_PEN
    try:
        canvas_mod.GRID_PEN = (QColor(0, 255, 200, 55), 1.0)
        win.toggle_grid(False)
        pump()
        base_faint = paint(c)
        win.toggle_grid(True)
        pump()
        faint = differing(base_faint, paint(c))
    finally:
        canvas_mod.GRID_PEN = now
    win.toggle_grid(False)
    pump()
    base_strong = paint(c)
    win.toggle_grid(True)
    pump()
    strong = differing(base_strong, paint(c))
    check("and it reads more strongly than the hairline it replaced",
          strong > faint * 2.0,
          f"ink {strong} now against {faint} for the old hairline "
          f"({strong / max(1, faint):.1f}x)")
    win.toggle_grid(False)
    pump()

    check("the page grid never drops to a single column",
          win.queue.columns_for_width(win.queue.width()) >= 2,
          f"{win.queue.columns_for_width(win.queue.width())} columns at "
          f"{win.queue.width()} px")
    check("and the panel enforces that as its own minimum",
          win.queue.minimumWidth() >= win.queue.width_for_columns(2),
          f"{win.queue.minimumWidth()} vs "
          f"{win.queue.width_for_columns(2)}")
    check("the splitter handle is wide enough to catch",
          win.splitter.handleWidth() >= 8, str(win.splitter.handleWidth()))
    check("and the panels cannot be collapsed to nothing by accident",
          not win.splitter.childrenCollapsible())

    # ------------------------------------------------------- un-marking done
    print("\nun-marking a page")
    win.queue.list.clearSelection()
    first = win.project.pages[0]
    win.queue.select(first.uid)
    pump()
    was = first.done
    win.toggle_done_selection()
    check("the done mark can be flipped on", first.done != was, str(first.done))
    win.toggle_done_selection()
    check("and flipped straight back off again", first.done == was,
          str(first.done))

    first.done = True
    win.queue.refresh_item(first.uid)
    win.toggle_done_selection()
    check("a finished page can be un-finished", not first.done)
    win.undo()
    pump()
    check("and undo puts the mark back", first.done, str(first.done))
    win.undo()
    pump()
    first.done = was
    win.queue.refresh_item(first.uid)

    # the inspector's own tick is undoable too - it was the one edit that
    # could not be taken back
    win.queue.select(first.uid)
    pump()
    steps = len(win.history._undo)
    win._set_done(not first.done)
    check("the inspector's done tick records an undo step",
          len(win.history._undo) > steps,
          f"{steps} -> {len(win.history._undo)}")
    win.undo()
    pump()
    check("so unticking it can be taken back", first.done == was,
          str(first.done))

    # ------------------------------------------------------------- cutting
    print("\ncutting and joining")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    page = win._current
    n_before = len(win.project.pages)
    win.canvas.select_all()
    pump()
    win.set_tool(Tool.CUT)
    check("the cut tool arms", win.canvas.tool == Tool.CUT)
    crop = win.canvas.crop_scene()
    mid = win.canvas.mapFromScene(QPointF(crop.center().x(), crop.center().y()))
    win.canvas.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(mid),
                                           Qt.LeftButton, Qt.LeftButton,
                                           Qt.NoModifier))
    win.canvas.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease,
                                             QPointF(mid), Qt.LeftButton,
                                             Qt.NoButton, Qt.NoModifier))
    pump()
    check("one scan becomes two pages",
          len(win.project.pages) == n_before + 1,
          f"{n_before} -> {len(win.project.pages)}")
    halves = [p for p in win.project.pages if p.path == page.path]
    check("both halves come from the same file", len(halves) == 2,
          str(len(halves)))
    check("they are told apart in the queue",
          {p.label for p in halves} == {"left", "right"},
          str([p.label for p in halves]))
    # the click lands on a whole screen pixel, which at fit zoom is several
    # scene pixels, so the halves are near-equal rather than exactly equal -
    # what must hold is that between them they account for the whole scan
    whole = page.output_size()[0]
    halved = [p.output_size()[0] for p in halves]
    check("the two halves account for the whole scan, with nothing lost",
          sum(halved) == whole, f"{halved} vs {whole}")
    check("and each is about half of it",
          all(abs(w - whole / 2) < whole * 0.02 for w in halved), str(halved))
    check("the queue rows differ",
          win.queue._label(halves[0]) != win.queue._label(halves[1]),
          win.queue._label(halves[0]))
    check("cutting is undoable", win.a_undo.isEnabled())
    win.undo()
    pump()
    check("undo puts the scan back together",
          len(win.project.pages) == n_before, str(len(win.project.pages)))

    # a measured cut, in one action rather than one per line
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.canvas.select_all()
    n_before = len(win.project.pages)
    win.canvas.cut_even(3, "v")
    pump()
    check("cut into thirds makes three pages",
          len(win.project.pages) == n_before + 2, str(len(win.project.pages)))
    thirds = [p for p in win.project.pages if p.label.endswith("/3")]
    widths = [p.output_size()[0] for p in thirds]
    check("the thirds are equal", len(thirds) == 3
          and max(widths) - min(widths) <= 1, str(widths))
    win.undo()
    pump()

    # joining, through the same alignment view the spread uses
    win.queue.list.clearSelection()
    for p in win.project.pages[:2]:
        win.queue.select(p.uid)
        win.queue.list.item(win.queue.list.currentRow()).setSelected(True)
    sel = win.queue.selected_uids()
    if len(sel) >= 2:
        left = win.project.by_uid(sel[0])
        right = win.project.by_uid(sel[1])
        n_before = len(win.project.pages)
        win._do_join(left, right, 0)
        pump()
        check("two scans become one page",
              len(win.project.pages) == n_before - 1, str(len(win.project.pages)))
        joined = next((p for p in win.project.pages if p.join), None)
        check("the joined page knows what it is made of",
              joined is not None and len(joined.parts()) == 2)
        if joined is not None:
            jw = joined.base_size()[0]
            check("it is as wide as both halves together",
                  jw >= left.output_size()[0] + right.output_size()[0] - 2,
                  f"{jw} vs {left.output_size()[0]}+{right.output_size()[0]}")
            check("a joined page is locked against the shared crop",
                  joined.crop_mode == "locked")
            win.queue.select(joined.uid)
            check("a joined page draws in the canvas",
                  wait_until(lambda: win._proxy is not None, 40)
                  and nonblank(paint(win.canvas)))
            win.unjoin_page()
            pump()
            check("and can be split back into the two scans",
                  len(win.project.pages) == n_before, str(len(win.project.pages)))

    # ---- what a joined page looks like everywhere it is shown
    # The fixture scans all look alike, so "a joined page draws" passed while
    # every preview was showing the *first* scan stretched across the joined
    # width: a joined page's `path` is only its first scan, and the aligner,
    # the book view and the page grid all read `path`.  Three flat scans at
    # different levels make the halves tell themselves apart.
    from scanforge import imageio_util as iio, render as rnd
    from scanforge.model import PageEdit
    from scanforge.ui.book_view import BookWindow
    flat_dir = os.path.join(TMP, "flat")
    os.makedirs(flat_dir, exist_ok=True)
    flat = []
    # Big enough that each scan's preview is scaled down to the 2200 px cap -
    # which is the real-world case.  At 1000 x 1400 nothing was scaled, every
    # preview sat at scale 1.0, and a joined page being drawn at a different
    # scale from the single scan beside it could not show up.
    for i, level in enumerate((40, 140, 230)):
        fp = os.path.join(flat_dir, f"flat_{i}.tif")
        iio.write_tiff(fp, np.full((4200, 3000, 3), level, np.uint8), dpi=600)
        proxycache.build(fp)
        flat.append(PageEdit(path=fp, src_w=3000, src_h=4200, dpi=600.0))
    kept_pages = list(win.project.pages)
    win.project.pages.extend(flat)
    win.queue.rebuild()
    pump()

    def means(arr, n):
        w = arr.shape[1]
        return [int(arr[:, k * w // n:(k + 1) * w // n].mean()) for k in range(n)]

    win._do_join(flat[0], flat[1], 0)
    pump()
    ab = next(p_ for p_ in win.project.pages if p_.join)
    win.queue.select(ab.uid)
    wait_until(lambda: win._proxy_raw is not None, 20)
    got = means(win._proxy_raw, 2) if win._proxy_raw is not None else None
    check("the canvas shows both scans of a joined page",
          got is not None and abs(got[0] - 40) < 4 and abs(got[1] - 140) < 4,
          str(got))

    lp, rp = win._spread_work(ab, flat[2])
    check("the aligner is handed the whole joined page, not its first scan",
          lp is not None and lp.shape[1] > lp.shape[0]
          and abs(means(lp, 2)[0] - 40) < 4 and abs(means(lp, 2)[1] - 140) < 4,
          f"{None if lp is None else lp.shape} {None if lp is None else means(lp, 2)}")

    # ...and draws it at the same scale as the single scan beside it.  The
    # joined page's preview was squeezed under the same cap as one scan, so a
    # page twice as wide arrived at a smaller scale and was drawn *shorter*
    # than the page it was being joined to.
    def drawn(lpv, rpv):
        pane_ = SpreadPane(ab, flat[2], lpv, rpv)
        L_, R_ = pane_.view.crop_spread("left"), pane_.view.crop_spread("right")
        scales_ = dict(pane_.view._scale)
        pane_.deleteLater()
        return L_, R_, scales_
    saved_crops = (ab.crop, flat[2].crop)
    L, R, sc = drawn(lp, rp)
    check("in the aligner a joined page is as tall as the single scan",
          abs(L.height() - R.height()) < 2,
          f"joined {L.width():.0f}x{L.height():.0f}, "
          f"single {R.width():.0f}x{R.height():.0f}")
    check("and twice as wide, because it is two scans",
          abs(L.width() - 2 * R.width()) < 3, f"{L.width():.0f} vs {R.width():.0f}")
    check("both pages are drawn at one scale", abs(sc["left"] - sc["right"]) < 1e-3,
          str(sc))
    check("the joined preview is built as sharp as its scans, not squeezed",
          abs(lp.shape[1] / 6000.0 - rp.shape[1] / 3000.0) < 0.002,
          f"{lp.shape[1] / 6000.0:.4f} vs {rp.shape[1] / 3000.0:.4f}")
    # and the aligner copes on its own if it is handed mismatched previews
    capped = rnd.base_preview(ab, "proxy")
    L, R, sc = drawn(capped, rp)
    check("the aligner evens out previews at different scales itself",
          abs(L.height() - R.height()) < 2 and abs(sc["left"] - sc["right"]) < 1e-3,
          f"joined {L.width():.0f}x{L.height():.0f}, "
          f"single {R.width():.0f}x{R.height():.0f}, {sc}")
    ab.crop, flat[2].crop = saved_crops

    bw = BookWindow(win.project, win)
    pm = bw.view._page_pixmap(win.project.index_of(ab.uid))
    img = pm.toImage() if pm is not None else None
    left_px = img.pixelColor(img.width() // 4, img.height() // 2).red() if img else None
    right_px = img.pixelColor(3 * img.width() // 4, img.height() // 2).red() if img else None
    check("the book view shows both scans, not the first one stretched",
          img is not None and abs(left_px - 40) < 4 and abs(right_px - 140) < 4
          and img.width() > img.height(),
          f"{None if img is None else (img.width(), img.height())} "
          f"left {left_px} right {right_px}")
    bw.close()

    win.queue.forget_thumbs()
    win.queue._thumb(ab)
    tpm = win.queue._thumb(ab).toImage()
    mid = tpm.height() // 2
    check("the page grid shows both scans too",
          abs(tpm.pixelColor(tpm.width() // 2 - 12, mid).red() - 40) < 6
          and abs(tpm.pixelColor(tpm.width() // 2 + 12, mid).red() - 140) < 6,
          f"{tpm.pixelColor(tpm.width() // 2 - 12, mid).red()} / "
          f"{tpm.pixelColor(tpm.width() // 2 + 12, mid).red()}")

    # a join of a join: the inner joined page has to assemble itself as well
    win._do_join(ab, flat[2], 0)
    pump()
    abc = next(p_ for p_ in win.project.pages if p_.join)
    check("a join of a joined page is as wide as all three scans",
          abc.base_size() == (9000, 4200), str(abc.base_size()))
    check("and knows every scan it is made from",
          abc.source_paths() == [f_.path for f_ in flat], str(abc.source_paths()))
    win.queue.select(abc.uid)
    wait_until(lambda: win._proxy_raw is not None
               and win._proxy_raw.shape[1] > win._proxy_raw.shape[0] * 1.8, 20)
    got = means(win._proxy_raw, 3) if win._proxy_raw is not None else None
    check("its preview shows all three scans in order",
          got is not None and all(abs(g - w_) < 5
                                  for g, w_ in zip(got, (40, 140, 230))),
          str(got))
    out = rnd.render(abc)
    check("and so does the full-resolution page",
          out.shape[:2] == (4200, 9000)
          and all(abs(g - w_) < 3 for g, w_ in zip(means(out, 3), (40, 140, 230))),
          f"{out.shape} {means(out, 3)}")

    win.project.pages[:] = kept_pages
    win.queue.rebuild()
    pump()

    # ------------------------------------------------------- size matching
    print("\ncrop size matching")
    from scanforge.ui.size_match_dialog import SizeMatchDialog
    for i, p in enumerate(win.project.pages[:3]):
        p.crop_mode = "linked"
        p.crop = [100.0, 100.0, 2000.0 + i, 3000.0 - i]
    smd = SizeMatchDialog(win.project, win)
    check("the dialog finds the near-identical group",
          smd.list.count() >= 1 and len(smd._groups[0]["pages"]) >= 3,
          str([(g["size"], len(g["pages"])) for g in smd._groups]))
    uids, size = smd.result_target()
    check("it proposes a size from the group itself",
          size in {tuple(s) for s in smd._groups[0]["sizes"]}, str(size))
    smd.sp_w.setValue(size[0] + 1)          # the +/- nudge
    uids, size = smd.result_target()
    n = win.project.match_sizes(uids, size)
    check("nudging the size and matching applies it",
          {win.project.by_uid(u).output_size() for u in uids} == {size},
          str({win.project.by_uid(u).output_size() for u in uids}))
    check("it reports how many it changed", n >= 3, str(n))
    smd.close()

    # ---------------------------------------------------------- book preview
    print("\nbook preview")
    from scanforge.ui.book_view import BookWindow, spreads_for
    check("a bound document opens with page one alone on the right",
          spreads_for(7, True) == [(None, 0), (1, 2), (3, 4), (5, 6)],
          str(spreads_for(7, True)))
    check("loose sheets pair from the first page",
          spreads_for(7, False) == [(0, 1), (2, 3), (4, 5), (6, None)],
          str(spreads_for(7, False)))
    check("an odd last page has nothing facing it",
          spreads_for(3, False)[-1] == (2, None), str(spreads_for(3, False)))
    check("no pages, no spreads", spreads_for(0) == [])

    win.open_book()
    pump(0.25)
    bk = win._book
    check("the reading window opens",
          isinstance(bk, BookWindow) and bk.isVisible())
    check("it starts on the cover", bk.view.current() == (None, 0),
          str(bk.view.current()))
    check("and draws the finished pages", nonblank(paint(bk.view)))
    bk.view.next_spread()
    pump(0.45)
    check("turning goes to the next pair", bk.view.current() == (1, 2),
          str(bk.view.current()))
    check("the label follows", "2" in bk.lbl.text() and "3" in bk.lbl.text(),
          bk.lbl.text())
    check("the slider follows too", bk.slider.value() == bk.view.spread,
          f"{bk.slider.value()} vs {bk.view.spread}")
    bk.view.prev_spread()
    pump(0.45)
    check("and back again", bk.view.current() == (None, 0),
          str(bk.view.current()))
    bk.chk_cover.setChecked(False)
    pump()
    check("turning the cover off re-pairs from page one",
          bk.view.spreads()[0] == (0, 1), str(bk.view.spreads()[0]))
    bk.view.set_zoom(2.0)
    check("it zooms", abs(bk.view.zoom - 2.0) < 1e-6, str(bk.view.zoom))
    check("and still paints when zoomed", nonblank(paint(bk.view)))
    bk.view.set_zoom(1.0)

    # one scale for the whole document, so the book does not breathe as it is
    # turned - and every spread inside the window, at any window shape
    scales = []
    for i in range(bk.view.count()):
        bk.view.spread = i
        scales.append(round(bk.view._scale(), 5))
    check("every spread is drawn at the same scale",
          len(set(scales)) == 1, str(scales))

    def worst_overflow():
        worst = -1e9
        for i in range(bk.view.count()):
            # measure each spread where it actually comes to rest, which for a
            # lone back cover is the middle of the window, not the spine
            bk.view._anim.stop()
            bk.view._turn_dir = 0
            bk.view.spread = i
            bk.view._sync_slide(animate=False)
            lr, rr, _l, _r = bk.view._rects_for(i)
            for r in (lr, rr):
                if r.width() <= 0:
                    continue
                worst = max(worst, -r.left(), -r.top(),
                            r.right() - bk.view.width(),
                            r.bottom() - bk.view.height())
        return worst

    for w, h in ((1100, 760), (620, 900), (1400, 460)):
        bk.resize(w, h)
        pump(0.15)
        check(f"the pages fit a {w}x{h} window", worst_overflow() <= 0,
              f"{worst_overflow():+.0f} px over")
    bk.resize(1100, 760)
    pump(0.15)

    # where the two covers stand: page one on the right of the spine, the way a
    # book falls open; the back cover, with nothing beside it, eases across to
    # the middle of the window instead of jumping there
    bk.chk_cover.setChecked(True)
    pump(0.2)
    bk.view._anim.stop()
    bk.view._turn_dir = 0
    bk.view.spread = 0
    bk.view._sync_slide(animate=False)
    lr, rr, _l, _r = bk.view._rects_for(0)
    check("the cover stands on the right of the spine",
          not lr.width() and rr.width()
          and abs(rr.left() - bk.view.spine_x()) < 1.0
          and rr.center().x() > bk.view.width() / 2,
          f"{rr.left():.0f}..{rr.right():.0f}, spine {bk.view.spine_x():.0f}")

    last = bk.view.count() - 1
    if bk.view._lone_left(last) is not None:
        bk.view.spread = last
        bk.view._slide_anim.stop()
        bk.view._slide_ref = bk.view._lone_left(last)
        travel = []
        for t in (0.0, 0.5, 1.0):
            bk.view._slide = t
            travel.append(bk.view._rects_for(last)[0].center().x())
        check("the back cover travels, it does not jump",
              travel[0] < travel[1] < travel[2],
              " -> ".join(f"{x:.0f}" for x in travel))
        bk.view._sync_slide(animate=False)
        cx = bk.view._rects_for(last)[0].center().x()
        check("and comes to rest in the middle of the window",
              abs(cx - bk.view.width() / 2) < 1.5,
              f"{cx:.0f} vs {bk.view.width() / 2:.0f}")
        # arriving at it from the previous spread animates the slide
        bk.view._anim.stop()
        bk.view._slide_anim.stop()
        bk.view._turn_dir = 0
        bk.view._slide = 0.0
        bk.view.spread = last - 1
        bk.view.next_spread()
        check("reaching the back cover starts the slide, not a snap",
              bk.view._slide_anim.state() == QAbstractAnimation.Running
              and bk.view._slide < 0.5,
              f"slide {bk.view._slide:.2f}")
        pump(0.8)
        check("and the slide finishes", abs(bk.view._slide - 1.0) < 1e-3,
              f"{bk.view._slide:.2f}")
        bk.view._anim.stop()
        bk.view._turn_dir = 0

    # clicking a page turns it: the left page back, the right page on.  The
    # cover goes back on so the middle spread is a real facing pair, and the
    # rectangles are measured again before each click - they move with the
    # spread, and a stale one would land on the wrong side of the spine.
    bk.chk_cover.setChecked(True)
    pump(0.2)

    def click_page(spread, side):
        bk.view._anim.stop()
        bk.view._turn_dir = 0
        bk.view.spread = spread
        pump(0.05)
        lr, rr, _l, _r = bk.view._rects_for(spread)
        assert lr.width() and rr.width(), f"spread {spread} is not a pair"
        at = (lr if side == "left" else rr).center()
        bk.view.mousePressEvent(QMouseEvent(
            QEvent.MouseButtonPress, QPointF(at), Qt.LeftButton,
            Qt.LeftButton, Qt.NoModifier))
        return bk.view.spread

    pair = next((i for i in range(bk.view.count())
                 if all(p is not None for p in bk.view.spreads()[i])), None)
    check("the book has a facing pair to click", pair is not None, str(pair))
    if pair is not None:
        got = click_page(pair, "left")
        check("clicking the left page goes back", got == pair - 1, str(got))
        got = click_page(pair, "right")
        check("clicking the right page goes on", got == pair + 1, str(got))
        pump(0.5)

    # a turn is one sheet: its front closes onto the spine, its back opens out
    bk.view._anim.stop()
    bk.view._turn_dir = 0
    bk.view.spread = 1
    bk.view.next_spread()
    check("turning a page animates", bk.view._turning)
    check("and knows which two spreads it is between",
          (bk.view._turn_from, bk.view._turn_to) == (1, 2),
          f"{bk.view._turn_from} -> {bk.view._turn_to}")
    seen = set()
    for t in (0.1, 0.3, 0.5, 0.7, 0.9):
        bk.view._turn_t = t
        seen.add(nonblank(paint(bk.view)))
    check("every phase of the turn draws", seen == {True}, str(seen))
    bk.view._anim.stop()
    bk.view._turn_dir = 0
    bk.view._turn_t = 0.0
    bk.view.spread = 0
    bk.view.go(bk.view.count() - 1, animate=False)
    check("a jump across the book does not animate", not bk.view._turning)

    bk.close()
    pump()

    # ------------------------------------------------------------- painting
    print("\npainting")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.toggle_grid(True)
    win.canvas.add_guide(True)
    win.canvas.add_guide(False)
    check("canvas paints with grid, guides and crop", nonblank(paint(win.canvas)))
    win.canvas.zoom_to(1.0)
    check("canvas paints at 100%", nonblank(paint(win.canvas)))
    win.toggle_grid(False)
    win.canvas.clear_guides()
    win.canvas.fit()

    # --------------------------------------------------------- automations
    print("\nautomation")
    idle = lambda: not win._busy                                   # noqa: E731
    win.reset_page()
    pump()

    # auto straighten + crop is the only automatic pass now: the deskew-only
    # button measured the same skew and then did less with it
    check("the deskew-only button is gone", not hasattr(win, "a_deskew")
          and not hasattr(win, "auto_deskew"))
    win.auto_both()
    check("the automatic pass runs off the GUI thread", "auto" in win._busy,
          str(win._busy))
    # a second slow action while one is running must be refused, not stacked
    win.fit_to_page()
    check("a second operation is refused while busy", "fitpage" not in win._busy,
          str(win._busy))
    check("the refusal is explained", "Busy" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())
    check("the automatic pass finishes", wait_until(idle, 30), "timed out")
    check("and it found the 0.4° skew", abs(win._current.angle + 0.4) < 0.05,
          f"{win._current.angle:+.3f}°")
    check("the strip followed the angle it set",
          abs(win.angle_strip.spin.value() - win._current.angle) < 1e-6,
          f"{win.angle_strip.spin.value():+.3f}°")

    win.fit_to_page()
    check("fit-to-page finishes", wait_until(idle, 30), "timed out")
    check("fit-to-page produced a crop", win._current.crop is not None)
    if win._current.crop:
        w, h = win._current.output_size()
        check("crop is inside the sheet", 2000 < w < 2260 and 2900 < h < 3260,
              f"{w}x{h} (sheet is 2220x3220)")
    # fit-to-page is a per-page action: it finds this sheet, and says nothing
    # about any other
    before_fit = {p.uid: p.output_size() for p in win.project.pages
                  if p is not win._current}
    check("fit-to-page changed only the page it ran on",
          all(p.output_size() == before_fit[p.uid]
              for p in win.project.pages if p is not win._current))
    # give the document one size again with the tool that exists for it, and
    # check the locked pages still sit out of the reckoning
    win.project.apply_crop_to_all(win._current)
    check("crop-all makes the document uniform again",
          win.project.uniform_size() is not None,
          str(win.project.uniform_size()))
    check("and uniform_size ignores the locked pages",
          win.project.uniform_size() == win._current.output_size(),
          f"{win.project.uniform_size()} vs {win._current.output_size()}")

    # -------------------------------------------------------------- project
    print("\nproject persistence")
    f = os.path.join(TMP, "session.sfproj")
    win.project.save(f)
    from scanforge.model import Project
    loaded = Project.load(f)
    check("project reloads all pages", len(loaded.pages) == len(win.project.pages))
    check("crops survive the round trip",
          loaded.pages[0].crop == win.project.pages[0].crop)
    check("the retired link setting is not written back out",
          not hasattr(loaded, "link_crop"))
    check("crop mode survives",
          [p.crop_mode for p in loaded.pages] ==
          [p.crop_mode for p in win.project.pages])

    win._dirty = False
    win.close()
    pump()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
