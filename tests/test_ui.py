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

from PySide6.QtCore import QAbstractAnimation, QPointF, Qt, QEvent
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

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
    check("two toolbars", win.toolbar_main is not None and win.toolbar_tools is not None)
    main_actions = [a.text().replace("&", "") for a in win.toolbar_main.actions()
                    if not a.isSeparator()]
    check("main bar order",
          main_actions == ["Open scan folder…", "Save project", "Save project as…",
                           "Export…", "Undo", "Redo", "Show grid"],
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
    check("queue rebuilt", win.queue.list.count() == 4, str(win.queue.list.count()))
    check("canvas paints something", nonblank(paint(win.canvas)))

    # -------------------------------------------------- first-crop + loading
    print("\nfirst crop and loading feedback")
    check("a fresh project arms the crop tool for you",
          win.canvas.tool == Tool.CROP, str(win.canvas.tool))
    e0 = win._current
    c0 = win.canvas
    # a click with no drag must not commit a one-pixel crop to the whole project
    c0.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(200, 200),
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    c0.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(201, 201),
                                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    check("a click without dragging leaves the crop alone",
          all(p.crop is None for p in win.project.pages),
          str([p.crop for p in win.project.pages if p.crop][:1]))
    check("and the crop tool stays armed for a real attempt",
          win.canvas.tool == Tool.CROP, str(win.canvas.tool))

    c0.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(80, 80),
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    for x, y in ((150, 180), (250, 320), (340, 420)):
        c0.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y),
                                      Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    c0.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(340, 420),
                                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    crops = {tuple(round(v) for v in p.crop) for p in win.project.pages if p.crop}
    check("that first crop reaches every page", len(crops) == 1, str(crops))
    check("and it is a real rectangle, not a stray pixel",
          all(c[2] > 50 and c[3] > 50 for c in crops), str(crops))
    check("and the tool releases itself afterwards",
          win.canvas.tool == Tool.CURSOR, str(win.canvas.tool))
    # The project-wide crop has now done the one job it exists for, so it
    # releases too - otherwise every later per-page nudge is copied silently
    # over the whole document, which is how careful work used to vanish.
    check("the project-wide crop releases itself after that first use",
          not win.project.link_crop)
    check("and the tool bar shows it off", not win.a_link.isChecked())
    check("the status bar explains why",
          "project-wide crop is now off" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())
    win.queue.select(win.project.pages[2].uid)
    wait_until(lambda: win._proxy is not None)
    others = [tuple(p.crop) for p in win.project.pages if p is not win._current]
    win.inspector.spins["x"].setValue(310)
    pump()
    check("a later crop change now stays on its own page",
          [tuple(p.crop) for p in win.project.pages
           if p is not win._current] == others,
          "other pages moved")

    # A bare click or a pan must not count as an edit.  It used to: every mouse
    # release emitted editChanged, which copied the current page's crop over
    # every other page - with no undo step, because a click opens none.
    win.toggle_link(True)
    before_click = [tuple(p.crop) for p in win.project.pages]
    win.project.pages[0].crop = [11.0, 22.0, 1500.0, 2100.0]
    c0.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(300, 300),
                                   Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    c0.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(300, 300),
                                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    check("clicking the canvas does not reshape the whole document",
          [tuple(p.crop) for p in win.project.pages[1:]] == before_click[1:],
          "a click propagated the crop")
    win.project.pages[0].crop = list(before_click[0])

    # and the release must survive undo: cropping, undoing, cropping again used
    # to leave the shared crop on for the rest of the session
    for p in win.project.pages:
        p.crop = None
    win.project.link_crop = True
    win._rearm_first_crop()
    check("with nothing cropped and the link on, the next crop is the first",
          win._first_crop_armed)
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.canvas.select_all()
    pump()
    check("drawing it releases the link again",
          not win.project.link_crop)

    # clearing every crop puts the project back to "nothing cropped yet", so
    # the next crop is the document's crop once more
    win.toggle_link(True)
    win.clear_crop()
    pump()
    check("clearing the crops re-arms the first-crop release",
          win._first_crop_armed and win.project.link_crop)

    # a page exempt from the shared crop cannot be the document's first crop:
    # cropping it propagates nothing, so releasing the link there would leave
    # every other page uncropped and the link switched off
    win.queue.select(win.project.pages[1].uid)
    wait_until(lambda: win._proxy is not None)
    win._current.crop_mode = "locked"
    win.canvas.select_all()
    pump()
    check("cropping a locked page does not release the shared crop",
          win.project.link_crop and win._first_crop_armed,
          f"link={win.project.link_crop} armed={win._first_crop_armed}")
    win._current.crop_mode = "linked"
    win._current.crop = None

    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.canvas.select_all()
    pump()
    check("but cropping an ordinary page still does",
          not win.project.link_crop)
    win.toggle_link(True)                    # back on for the tests that follow
    win.queue.select(win.project.pages[1].uid)
    wait_until(lambda: win._proxy is not None)
    check("it does not re-arm on later pages",
          win.canvas.tool == Tool.CURSOR, str(win.canvas.tool))

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

    # a stray drag in cursor mode must not start a new crop
    c = win.canvas
    press = QMouseEvent(QEvent.MouseButtonPress, QPointF(60, 60), Qt.LeftButton,
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

    win.set_tool(Tool.STRAIGHTEN)
    win.escape()
    check("Esc returns to cursor", win.canvas.tool == Tool.CURSOR)

    check("tool bar order: selection, page, auto, zoom, done",
          [a.text().replace("&", "") for a in win.toolbar_tools.actions()
           if not a.isSeparator()] ==
          ["Cursor", "Crop", "Place alignment guide", "Blur a spot",
           "Cut into pages",
           "Same crop for every page", "Copy framing from previous",
           "Match crop sizes…", "Align as spread…", "Join pages into one…",
           "Auto straighten  crop", "Auto-deskew page",
           "Fit crop to page edges", "Fit page", "100%  (actual pixels)",
           "Preview as a book…", "Mark done and go to next"],
          str([a.text().replace("&", "") for a in win.toolbar_tools.actions()
               if not a.isSeparator()]))

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
    check("inspector shows the blur", win.inspector.sld_blur.isEnabled())

    b = page.blurs[0]
    check("blur has a sane default strength", 2 <= b[4] <= 300, str(b[4]))
    win.inspector.sp_blur.setValue(40)
    pump()
    check("strength follows the inspector", abs(page.blurs[0][4] - 40) < 1e-6,
          str(page.blurs[0][4]))
    win.inspector.cmb_blur_mode.setCurrentText("Pixelate")
    pump()
    check("style follows the inspector", page.blurs[0][5] == "Pixelate",
          str(page.blurs[0][5]))

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

    # --------------------------------------------------------- linked crop
    print("\nlinked crop")
    # it was switched back on by hand after the first crop released it, which
    # is exactly the journey a real project takes
    win.toggle_link(True)
    check("linking can be switched back on", win.project.link_crop)
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.inspector.spins["w"].setValue(1800)
    win.inspector.spins["h"].setValue(2600)
    pump()
    sizes = {p.output_size() for p in win.project.pages}
    check("every page took the new crop size", sizes == {(1800, 2600)}, str(sizes))
    check("queue reports one uniform size",
          win.project.uniform_size() == (1800, 2600), str(win.project.uniform_size()))

    win.queue.select(win.project.pages[2].uid)
    wait_until(lambda: win._proxy is not None)
    win.inspector.spins["x"].setValue(120)
    pump()
    xs = {round(p.crop[0]) for p in win.project.pages}
    check("position propagates too", xs == {120}, str(xs))

    win.project.pages[1].crop_mode = "own_position"
    win.project.pages[1].crop = [999.0, 888.0, 1800.0, 2600.0]
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.inspector.spins["x"].setValue(200)
    pump()
    p1 = win.project.pages[1]
    check("an independent page keeps its own position", round(p1.crop[0]) == 999,
          str(p1.crop[0]))
    check("but is forced to the shared size", p1.output_size() == (1800, 2600),
          str(p1.output_size()))

    locked = win.project.pages[3]
    locked.crop_mode = "locked"
    locked.crop = [7.0, 8.0, 1234.0, 5678.0]
    keep = list(locked.crop)
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.inspector.spins["x"].setValue(321)
    pump()
    check("a locked page resists the shared crop", locked.crop == keep,
          str(locked.crop))
    locked.crop_mode = "linked"
    locked.crop = list(win.project.pages[0].crop)     # back in step with the rest

    win.toggle_link(False)
    check("link can be turned off", not win.project.link_crop)
    win.toggle_link(True)

    # ------------------------------------------------------------ undo/redo
    print("\nundo / redo")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    before_angle = win._current.angle
    win.inspector.spin_angle.setValue(-1.25)
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
    sizes = win.splitter.sizes()
    win.splitter.setSizes([q.width_for_columns(2) + 17, sizes[1] - 17, sizes[2]])
    win._snap_queue_width()
    pump()
    check("dragging the divider lands it on a column boundary",
          win.splitter.sizes()[0] == q.width_for_columns(2),
          f"{win.splitter.sizes()[0]} vs {q.width_for_columns(2)}")

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
    dlg2.close()

    # ---------------------------------------------------------------- spread
    print("\nspread alignment")
    win.queue.list.clearSelection()
    win.queue.select(win.project.pages[2].uid)
    pump()
    win.open_spread()
    check("one selection starts a pick, not an error",
          win._spread_pick == win.project.pages[2].uid, str(win._spread_pick))
    check("status bar asks for the facing page",
          "click the page that faces" in win.statusBar().currentMessage(),
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
    check("the queue and the inspector are still there",
          win.queue.isVisible() and win.inspector.isVisible())
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
        win.project.apply_linked_crop(other_page)
        check("and the shared crop no longer destroys the alignment",
              dlg.left.crop == keep_l and dlg.right.crop == keep_r,
              f"{dlg.left.crop} / {dlg.right.crop}")
        check("pages stayed separate files", dlg.left.path != dlg.right.path)
        check("finishing puts the canvas back",
              win._mode is None and win.center.currentWidget() is win.canvas)

    # -------------------------------------------------- alignment guide
    print("\nalignment guide")
    win.queue.select(win.project.pages[0].uid)
    wait_until(lambda: win._proxy is not None)
    win.canvas.fit()
    pump()
    c = win.canvas
    win.set_tool(Tool.ALIGN)
    check("the guide tool arms", c.tool == Tool.ALIGN, str(c.tool))
    c.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(120, 300),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    for x in (200, 320, 420):
        c.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, 303),
                                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    angle_before = win._current.angle
    c.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(420, 303),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    pump()
    check("the guide stays on the canvas after the drag",
          c.align_line is not None)
    check("drawing it does NOT rotate the page — you turn the page onto it",
          win._current.angle == angle_before,
          f"{angle_before} -> {win._current.angle}")
    check("a nearly-level guide snaps to the axis", c.align_axis == "h",
          f"axis={c.align_axis!r} angle={c.guide_angle():.4f}")
    a, b = c.guide_points()
    check("...and is therefore exactly level", abs(a.y() - b.y()) < 1e-9,
          f"{a.y():.4f} vs {b.y():.4f}")
    check("the guide tool releases like the others", c.tool == Tool.CURSOR)
    check("canvas paints with the guide on it", nonblank(paint(c)))

    # The guide belongs to the editor window, not to the page: it is a
    # straightedge held against the screen, so rotating, panning or zooming
    # moves the page underneath while the guide stays exactly where it was put.
    where_before = c.align_line
    angle_before = c.guide_angle()
    win._set_angle(win._current.angle + 0.4)
    pump()
    check("rotating the page does not move the guide",
          c.align_line == where_before, str(c.align_line))
    check("the guide keeps its own angle while the page turns",
          abs(c.guide_angle() - angle_before) < 1e-9,
          f"{angle_before:.5f} -> {c.guide_angle():.5f}")
    c.zoom_by(1.7)
    c.horizontalScrollBar().setValue(c.horizontalScrollBar().value() + 60)
    pump()
    check("zooming and panning do not move it either",
          c.align_line == where_before and abs(c.guide_angle()) < 1e-9,
          str(c.align_line))
    win.canvas.fit()
    pump()

    # dragging it by the middle moves the whole line
    at = c.guide_view_points()
    scr = QPointF((at[0].x() + at[1].x()) / 2.0,
                  (at[0].y() + at[1].y()) / 2.0)
    c.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(scr),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    grabbed = c._drag[0] if c._drag else None
    c.mouseMoveEvent(QMouseEvent(QEvent.MouseMove,
                                 QPointF(scr.x(), scr.y() + 40),
                                 Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    c.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease,
                                    QPointF(scr.x(), scr.y() + 40),
                                    Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    check("the guide can be picked up and moved", grabbed == "guide_move",
          str(grabbed))
    check("moving it does not rotate anything either",
          abs(c.guide_angle()) < 1e-6, f"{c.guide_angle():.5f}")

    # a deliberately crooked guide, and the one-press way to meet it
    vw, vh = c.viewport().width(), c.viewport().height()
    c.set_guide_points(QPointF(vw * 0.2, vh * 0.5),
                       QPointF(vw * 0.8, vh * 0.5 + vw * 0.6 * 0.01), "")
    before = win._current.angle
    delta = c.rotate_page_to_guide()
    check("rotate-to-guide turns the page by the guide's own slope",
          abs(delta) > 1e-3
          and abs(win._current.angle - (before + delta)) < 2e-3,
          f"{delta:+.3f}° — page went {before:+.3f}° to {win._current.angle:+.3f}°")
    c.clear_guide()
    check("the guide can be cleared", c.align_line is None)

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

    win.auto_deskew()
    check("auto-deskew runs off the GUI thread", "deskew" in win._busy,
          str(win._busy))
    # a second slow action while one is running must be refused, not stacked
    win.fit_to_page()
    check("a second operation is refused while busy", "fitpage" not in win._busy,
          str(win._busy))
    check("the refusal is explained", "Busy" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())
    check("auto-deskew finishes", wait_until(idle, 30), "timed out")
    check("auto-deskew found the 0.4° skew", abs(win._current.angle + 0.4) < 0.05,
          f"{win._current.angle:+.3f}°")

    win.fit_to_page()
    check("fit-to-page finishes", wait_until(idle, 30), "timed out")
    check("fit-to-page produced a crop", win._current.crop is not None)
    if win._current.crop:
        w, h = win._current.output_size()
        check("crop is inside the sheet", 2000 < w < 2260 and 2900 < h < 3260,
              f"{w}x{h} (sheet is 2220x3220)")
    # the spread pages are locked by now, so they are meant to differ
    following = {p.output_size() for p in win.project.pages
                 if p.crop_mode != "locked"}
    check("fit-to-page propagated to every page that follows the shared crop",
          len(following) == 1, str(following))
    check("uniform_size ignores the locked pages",
          win.project.uniform_size() is not None,
          str(win.project.uniform_size()))

    # -------------------------------------------------------------- project
    print("\nproject persistence")
    f = os.path.join(TMP, "session.sfproj")
    win.project.save(f)
    from scanforge.model import Project
    loaded = Project.load(f)
    check("project reloads all pages", len(loaded.pages) == len(win.project.pages))
    check("crops survive the round trip",
          loaded.pages[0].crop == win.project.pages[0].crop)
    check("link setting survives", loaded.link_crop == win.project.link_crop)
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
