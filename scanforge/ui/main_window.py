"""ScanForge main window."""

from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QSettings, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence, QPalette
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QMainWindow,
                               QMessageBox, QSplitter, QStackedWidget,
                               QStatusBar, QToolBar, QWidget)

from .. import autodetect, imageio_util as iio, proxycache, render as rnd
from ..export import BatchExporter, ExportSettings
from ..geometry import Rect
from ..history import History
from .. import model as mdl
from ..model import PageEdit, Project
from . import icons
from .canvas import Canvas, Tool
from .export_dialog import ExportDialog, ProgressDialog
from .inspector import Inspector
from .loader import AsyncLoader, Background
from .loading_dialog import QueueLoadDialog
from .overlays import CanvasNotice, Throbber
from .queue_panel import QueuePanel, gather_paths
from .book_view import BookWindow
from .spread_view import SpreadPane

APP = "ScanForge"
ORG = "ScanForge"
ICON_PX = 22


class _CacheBridge(QObject):
    """Carries prefetch progress from the worker threads to the GUI thread."""
    started = Signal(str, object)         # path, tiers
    finished = Signal(str, object, bool)  # path, tiers, ok
    progress = Signal(str, float)         # path, fraction of its decode done


def dark_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.Window, QColor(43, 45, 50))
    p.setColor(QPalette.WindowText, QColor(214, 218, 224))
    p.setColor(QPalette.Base, QColor(33, 35, 40))
    p.setColor(QPalette.AlternateBase, QColor(48, 50, 56))
    p.setColor(QPalette.Text, QColor(214, 218, 224))
    p.setColor(QPalette.Button, QColor(53, 56, 62))
    p.setColor(QPalette.ButtonText, QColor(214, 218, 224))
    p.setColor(QPalette.Highlight, QColor(52, 111, 168))
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ToolTipBase, QColor(40, 42, 47))
    p.setColor(QPalette.ToolTipText, QColor(220, 224, 230))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 124, 132))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 124, 132))
    return p


TOOLBAR_QSS = """
QToolBar { background:#2f3238; border:0; border-bottom:1px solid #23252a;
           padding:3px 6px; spacing:2px; }
QToolBar::separator { background:#4a4e56; width:1px; margin:4px 7px; }
QToolButton { background:transparent; border:1px solid transparent;
              border-radius:5px; padding:4px; }
QToolButton:hover { background:#3c4048; border-color:#4c515a; }
QToolButton:pressed { background:#2a2d33; }
QToolButton:checked { background:#3a5f8a; border-color:#4f7dae; }
QToolButton:disabled { }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP)
        self.resize(1600, 980)
        self.setAcceptDrops(True)

        self.settings = QSettings(ORG, APP)
        self.project = Project()
        self.history = History(self.project)
        self.export_cfg = ExportSettings()
        # sizes itself from the machine's free memory, then again from the
        # actual scan dimensions once the headers have been read
        self.prefetch = proxycache.Prefetcher()
        self.loader = AsyncLoader(workers=3, parent=self)
        self.bg = Background(workers=2, parent=self)
        # names of operations currently running off-thread; a second attempt
        # while one is in flight is refused rather than stacked
        self._busy: set = set()

        self._current: Optional[PageEdit] = None
        self._proxy_raw: Optional[np.ndarray] = None   # exactly as decoded
        self._proxy: Optional[np.ndarray] = None       # after 90s and flips
        self._detail_loaded = False
        self._dirty = False
        self._spread_pick: Optional[str] = None
        self._first_crop_pending = True
        self._first_crop_armed = False

        # -------------------------------------------------------- widgets
        self.canvas = Canvas(self)
        self.inspector = Inspector(self)
        self.queue = QueuePanel(self.project, self.prefetch, self)

        # The working area is a stack: the editing canvas, and whatever mode
        # temporarily takes its place - aligning a spread, for instance, which
        # used to open in a floating window and put the work somewhere other
        # than where you were working.
        self.center = QStackedWidget()
        self.center.addWidget(self.canvas)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.queue)
        split.addWidget(self.center)
        split.addWidget(self.inspector)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([300, 1000, 340])
        split.splitterMoved.connect(self._snap_queue_width)
        self.setCentralWidget(split)
        self.splitter = split

        # a panel that floats over the canvas for waits and for modes that
        # need an instruction and a way out
        self.notice = CanvasNotice(self.canvas)
        self.canvas.installEventFilter(self)
        self._cache_bridge = _CacheBridge()
        self._cache_bridge.started.connect(self._cache_started)
        self._cache_bridge.finished.connect(self._cache_finished)
        self._cache_bridge.progress.connect(self._cache_progress)
        self.prefetch.start_listeners.append(
            lambda path, tiers: self._cache_bridge.started.emit(path, tiers))
        self.prefetch.listeners.append(
            lambda path, tiers, ok:
                self._cache_bridge.finished.emit(path, tiers, ok))
        self.prefetch.progress_listeners.append(
            lambda p, f: self._cache_bridge.progress.emit(p, f))
        self._load_dialog: Optional[QueueLoadDialog] = None
        self._closing = False
        self._mode = None            # a pane occupying the work area

        # timers must exist before anything is wired to them
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(300)
        self._detail_timer.timeout.connect(self._maybe_load_detail)

        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(700)
        self._history_timer.timeout.connect(self.history.commit)

        # Relabelling the whole queue after every propagated crop change would
        # make a spin box scrub feel gluey once a project runs to hundreds of
        # pages, so the refresh is coalesced into one pass.
        self._queue_timer = QTimer(self)
        self._queue_timer.setSingleShot(True)
        self._queue_timer.setInterval(160)
        self._queue_timer.timeout.connect(self._refresh_queue_now)

        self._autosave = QTimer(self)
        self._autosave.setInterval(45000)
        self._autosave.timeout.connect(self._autosave_project)
        self._autosave.start()

        self._build_actions()
        self._build_toolbars()
        self._build_statusbar()
        self._wire()

        self.history.changed = self._history_changed
        self._history_changed()
        self._restore_geometry()
        self.canvas.set_message("Drop a folder of scans here, or press Ctrl+O")

    # ------------------------------------------------------------- actions
    def _act(self, text, slot, shortcut=None, tip=None, checkable=False,
             icon: str = "", app_shortcut: bool = False):
        a = QAction(text, self)
        if icon:
            a.setIcon(icons.icon(icon, ICON_PX))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
            if app_shortcut:
                a.setShortcutContext(Qt.ApplicationShortcut)
        label = tip or text.replace("&", "")
        a.setToolTip(f"{label}   ({shortcut})" if shortcut else label)
        a.setStatusTip(label)
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def _build_actions(self):
        # ---- file
        self.a_open_folder = self._act("Open scan &folder…", self.open_folder,
                                       "Ctrl+O", "Open a folder of scans",
                                       icon="open")
        self.a_new = self._act("&New project", lambda: self.new_project(),
                               "Ctrl+N", "Clear everything and start again")
        self.a_add_files = self._act("&Add files…", self.add_files, "Ctrl+Shift+O")
        self.a_load = self._act("Open &project…", self.load_project, "Ctrl+Alt+O")
        self.a_save = self._act("&Save project", self.save_project, "Ctrl+S",
                                "Save project", icon="save")
        self.a_save_as = self._act("Save project &as…", self.save_project_as,
                                   "Ctrl+Shift+S", "Save project as", icon="save_as")
        self.a_export = self._act("&Export…", self.export, "Ctrl+E",
                                  "Export every page at full resolution",
                                  icon="export")
        self.a_export_sel = self._act("Export &selected…", self.export_selection,
                                      "Ctrl+Shift+E")
        self.a_quit = self._act("E&xit", self.close, "Ctrl+Q")

        # ---- history.  Application-wide, so a focused spin box cannot swallow
        # Ctrl+Z and leave the app in a half-edited state.
        self.a_undo = self._act("&Undo", self.undo, "Ctrl+Z", "Undo",
                                icon="undo", app_shortcut=True)
        self.a_redo = self._act("&Redo", self.redo, "Ctrl+Y", "Redo",
                                icon="redo", app_shortcut=True)
        self.a_redo2 = self._act("Redo", self.redo, "Ctrl+Shift+Z",
                                 app_shortcut=True)

        # ---- edit
        self.a_deskew = self._act("&Auto-deskew page", self.auto_deskew, "D",
                                  "Estimate the skew from the text lines",
                                  icon="deskew")
        self.a_fitpage = self._act("Fit crop to page &edges", self.fit_to_page, "E",
                                   "Detect the sheet and crop to it", icon="fitpage")
        self.a_auto_both = self._act("Auto straighten && crop", self.auto_both, "A",
                                     "Straighten and crop in one go", icon="auto")
        self.a_clear_crop = self._act("&Clear crop", self.clear_crop, "Ctrl+D")
        self.a_reset = self._act("&Reset page", self.reset_page, "Ctrl+R")
        self.a_copy_prev = self._act("Copy framing from &previous", self.copy_previous,
                                     "Ctrl+B", "Copy framing from the previous page",
                                     icon="copyframe")
        self.a_apply_doc = self._act("Apply framing to &all pages", self.apply_to_document)
        self.a_apply_sel = self._act("Apply framing to &selection",
                                     self.apply_to_selection, "Ctrl+Shift+B")
        self.a_set_doc = self._act("Set document &name…", self.set_document_name,
                                   "Ctrl+G",
                                   "One name for the whole set, used for every "
                                   "exported file")
        self.a_link = self._act("Same crop for &every page", self.toggle_link,
                                tip="Keep one crop across the whole project so "
                                    "every exported page is identical in size",
                                checkable=True, icon="link")
        self.a_link.setChecked(True)

        # ---- view
        self.a_fit = self._act("&Fit page", self.canvas.fit, "F", "Fit page",
                               icon="fitview")
        self.a_100 = self._act("&100%  (actual pixels)", lambda: self.canvas.zoom_to(1.0),
                               "1", "Zoom to actual pixels", icon="one2one")
        self.a_50 = self._act("50%", lambda: self.canvas.zoom_to(0.5), "2")
        self.a_zoomin = self._act("Zoom in", lambda: self.canvas.zoom_by(1.25), "Ctrl+=")
        self.a_zoomout = self._act("Zoom out", lambda: self.canvas.zoom_by(0.8), "Ctrl+-")
        self.a_grid = self._act("Show &grid", self.toggle_grid, "G", "Show grid",
                                checkable=True, icon="grid")
        self.a_thirds = self._act("Show &thirds", self.toggle_thirds, checkable=True)
        self.a_thirds.setChecked(True)
        self.a_dim = self._act("&Dim outside crop", self.toggle_dim, checkable=True)
        self.a_dim.setChecked(True)
        self.a_guide_h = self._act("Add &horizontal guide",
                                   lambda: self.canvas.add_guide(True), "H")
        self.a_guide_v = self._act("Add &vertical guide",
                                   lambda: self.canvas.add_guide(False), "Shift+H")
        self.a_guide_clear = self._act("Clear guides", self.canvas.clear_guides)

        # ---- page
        self.a_next = self._act("&Next page", lambda: self.step(1), "Ctrl+Right")
        self.a_prev = self._act("&Previous page", lambda: self.step(-1), "Ctrl+Left")
        self.a_done_next = self._act("Mark &done and go to next", self.done_and_next,
                                     "Return", "Mark done and go to next",
                                     icon="done")
        self.a_spread = self._act("Align as &spread…", self.open_spread, "Ctrl+J",
                                  "Align two facing pages", icon="spread")
        self.a_remove = self._act("&Remove from queue", self.remove_selected, "Del")
        self.a_up = self._act("Move page &up", lambda: self.queue._move(-1),
                              "Ctrl+Shift+Up")
        self.a_down = self._act("Move page &down", lambda: self.queue._move(1),
                                "Ctrl+Shift+Down")
        self.a_move_to = self._act("&Move to position…",
                                   lambda: self.queue.move_to_position(), "Ctrl+M",
                                   "Move the selected pages to a chosen position")
        self.a_cancel = self._act("Cancel", self.escape, "Esc")

        # ---- tools
        self.a_tool_cursor = self._act("&Cursor", lambda: self.set_tool(Tool.CURSOR),
                                       "V", "Cursor — pan, zoom, adjust an "
                                            "existing crop", checkable=True,
                                       icon="cursor")
        self.a_tool_crop = self._act("&Crop", lambda: self.set_tool(Tool.CROP), "C",
                                     "Draw a new crop (one use, then back to cursor)",
                                     checkable=True, icon="crop")
        self.a_tool_line = self._act("Place &alignment guide",
                                     lambda: self.set_tool(Tool.ALIGN), "L",
                                     "Lay down a straightedge, then turn the "
                                     "page onto it", checkable=True, icon="level")
        self.a_guide_rotate = self._act("Rotate page to &guide",
                                        lambda: self.canvas.rotate_page_to_guide(),
                                        "Ctrl+L",
                                        "Turn the page so the guide becomes level")
        self.a_guide_drop = self._act("Clear alignment guide",
                                      lambda: self.canvas.clear_guide(), "Shift+L")
        self.a_tool_blur = self._act("&Blur a spot",
                                     lambda: self.set_tool(Tool.BLUR), "B",
                                     "Drag out a blur over something on the page",
                                     checkable=True, icon="blur")
        self.a_blur_delete = self._act("Delete the selected blur",
                                       lambda: self.canvas.delete_blur(), "Ctrl+Del")
        self.a_tool_cut = self._act("C&ut into pages",
                                    lambda: self.set_tool(Tool.CUT), "X",
                                    "Split this scan into separate pages along "
                                    "a line you place", checkable=True, icon="cut")
        self.a_cut_halves = self._act(
            "Cut into halves", lambda: self.canvas.cut_even(2, "v"), "",
            "Two pages of equal width")
        self.a_cut_thirds = self._act(
            "Cut into thirds", lambda: self.canvas.cut_even(3, "v"), "",
            "Three pages of equal width")
        self.a_cut_quarters = self._act(
            "Cut into quarters", lambda: self.canvas.cut_even(4, "v"), "")
        self.a_cut_halves_h = self._act(
            "Cut across the middle", lambda: self.canvas.cut_even(2, "h"), "",
            "Two pages of equal height")
        self.a_cut_thirds_h = self._act(
            "Cut across into thirds", lambda: self.canvas.cut_even(3, "h"), "")
        self.a_join = self._act("&Join pages into one…", self.join_pages, "Ctrl+Shift+J",
                                "Align two scans and stitch them into a single page",
                                icon="join")
        self.a_unjoin = self._act("Split a joined page apart", self.unjoin_page, "")
        self.a_book = self._act("&Preview as a book…", self.open_book, "Ctrl+P",
                                "Read the finished pages two at a time, to "
                                "check the order and how facing pages sit",
                                icon="book")
        self.a_match_sizes = self._act("&Match crop sizes…", self.match_sizes,
                                       "Ctrl+Shift+M",
                                       "Bring crops that are within a few pixels "
                                       "of each other onto one exact size",
                                       icon="matchsize")
        grp = QActionGroup(self)
        for a in (self.a_tool_cursor, self.a_tool_crop, self.a_tool_line,
                  self.a_tool_blur, self.a_tool_cut):
            grp.addAction(a)
        self.a_tool_cursor.setChecked(True)

        self.a_cache = self._act("Clear proxy cache", self.clear_cache)

        # ---- menus
        m_file = self.menuBar().addMenu("&File")
        for a in (self.a_new, self.a_open_folder, self.a_add_files, None, self.a_load,
                  self.a_save, self.a_save_as, None, self.a_export,
                  self.a_export_sel, None, self.a_quit):
            m_file.addSeparator() if a is None else m_file.addAction(a)

        m_edit = self.menuBar().addMenu("&Edit")
        for a in (self.a_undo, self.a_redo, None, self.a_deskew, self.a_fitpage,
                  self.a_auto_both, None, self.a_clear_crop, self.a_reset, None,
                  self.a_link, self.a_copy_prev, self.a_apply_doc,
                  self.a_apply_sel, None, self.a_set_doc):
            m_edit.addSeparator() if a is None else m_edit.addAction(a)

        m_view = self.menuBar().addMenu("&View")
        for a in (self.a_book, None,
                  self.a_fit, self.a_100, self.a_50, self.a_zoomin, self.a_zoomout,
                  None, self.a_grid, self.a_thirds, self.a_dim, None,
                  self.a_guide_h, self.a_guide_v, self.a_guide_clear):
            m_view.addSeparator() if a is None else m_view.addAction(a)

        m_page = self.menuBar().addMenu("&Page")
        for a in (self.a_prev, self.a_next, self.a_done_next, None, self.a_up,
                  self.a_down, self.a_move_to, None, self.a_spread, self.a_join,
                  self.a_unjoin, None, self.a_remove):
            m_page.addSeparator() if a is None else m_page.addAction(a)
        m_cut = m_page.addMenu("&Cut into pages")
        for a in (self.a_tool_cut, None, self.a_cut_halves, self.a_cut_thirds,
                  self.a_cut_quarters, None, self.a_cut_halves_h,
                  self.a_cut_thirds_h):
            m_cut.addSeparator() if a is None else m_cut.addAction(a)

        m_tools = self.menuBar().addMenu("&Tools")
        for a in (self.a_tool_cursor, self.a_tool_crop, self.a_tool_line,
                  self.a_tool_blur, self.a_tool_cut, None, self.a_guide_rotate,
                  self.a_guide_drop, self.a_blur_delete, None,
                  self.a_match_sizes, None, self.a_cache):
            m_tools.addSeparator() if a is None else m_tools.addAction(a)

        m_help = self.menuBar().addMenu("&Help")
        m_help.addAction(self._act("&Shortcuts", self.show_help, "F1"))

    def _build_toolbars(self):
        self.setStyleSheet(TOOLBAR_QSS)

        main = QToolBar("Main")
        main.setObjectName("mainbar")
        main.setMovable(False)
        main.setToolButtonStyle(Qt.ToolButtonIconOnly)
        main.setIconSize(QSize(ICON_PX, ICON_PX))
        for a in (self.a_open_folder, self.a_save, self.a_save_as, self.a_export,
                  None, self.a_undo, self.a_redo, None, self.a_grid):
            main.addSeparator() if a is None else main.addAction(a)
        self.addToolBar(main)
        self.toolbar_main = main

        self.addToolBarBreak()

        tools = QToolBar("Tools")
        tools.setObjectName("toolbar")
        tools.setMovable(False)
        tools.setToolButtonStyle(Qt.ToolButtonIconOnly)
        tools.setIconSize(QSize(ICON_PX, ICON_PX))
        # selection tools | page tools | auto tools | zoom | done
        for a in (self.a_tool_cursor, self.a_tool_crop, self.a_tool_line,
                  self.a_tool_blur, self.a_tool_cut,
                  None, self.a_link, self.a_copy_prev, self.a_match_sizes,
                  self.a_spread, self.a_join,
                  None, self.a_auto_both, self.a_deskew, self.a_fitpage,
                  None, self.a_fit, self.a_100, self.a_book,
                  None, self.a_done_next):
            tools.addSeparator() if a is None else tools.addAction(a)
        self.addToolBar(tools)
        self.toolbar_tools = tools

    def _build_statusbar(self):
        sb = QStatusBar()
        self.throbber = Throbber(14)
        sb.addPermanentWidget(self.throbber)
        self.lbl_pos = QLabel("—")
        self.lbl_zoom = QLabel("")
        self.lbl_progress = QLabel("")
        for w in (self.lbl_progress, self.lbl_pos, self.lbl_zoom):
            w.setStyleSheet("color:#9aa0aa;padding:0 10px;")
            sb.addPermanentWidget(w)
        self.setStatusBar(sb)

    def _wire(self):
        c = self.canvas
        c.editChanged.connect(self._canvas_edited)
        c.statusMessage.connect(self.lbl_zoom.setText)
        c.cursorMoved.connect(self._cursor_moved)
        c.interactionStarted.connect(self._begin_history)
        c.interactionEnded.connect(self._end_history)
        c.toolFinished.connect(lambda: self.set_tool(Tool.CURSOR))
        c.blurSelected.connect(self._blur_selected)
        c.cutRequested.connect(self._do_cut)
        for sb in (c.horizontalScrollBar(), c.verticalScrollBar()):
            sb.valueChanged.connect(lambda _v: self._detail_timer.start())

        i = self.inspector
        i.angleChanged.connect(self._set_angle)
        i.cropChanged.connect(self._set_crop)
        i.orientationChanged.connect(self._set_orientation)
        i.requestAutoDeskew.connect(self.auto_deskew)
        i.requestFitToPage.connect(self.fit_to_page)
        i.requestStraightenTool.connect(lambda: self.set_tool(Tool.ALIGN))
        i.requestCropTool.connect(lambda: self.set_tool(Tool.CROP))
        i.requestBlurTool.connect(lambda: self.set_tool(Tool.BLUR))
        i.requestBlurDelete.connect(lambda: self.canvas.delete_blur())
        i.blurChanged.connect(
            lambda radius, mode: self.canvas.set_blur_property(radius, mode))
        i.requestResetCrop.connect(self.clear_crop)
        i.requestSelectAll.connect(self.select_all_crop)
        i.requestCopyPrevious.connect(self.copy_previous)
        i.requestApplyToDocument.connect(self.apply_to_document)
        i.requestApplyToSelection.connect(self.apply_to_selection)
        i.aspectChanged.connect(lambda r: setattr(self.canvas, "lock_aspect", r))
        i.doneToggled.connect(self._set_done)
        i.linkToggled.connect(self._set_link)
        i.cropModeChanged.connect(self._set_crop_mode)

        q = self.queue
        q.pageActivated.connect(self.show_page)
        q.pagesDropped.connect(self.add_paths)
        q.removeRequested.connect(self.remove_uids)
        q.orderChanged.connect(self.history.capture)
        q.setDocumentRequested.connect(self.set_document_name)
        q.spreadRequested.connect(self.open_spread)
        q.joinRequested.connect(self.join_pages)
        q.unjoinRequested.connect(self.unjoin_page)

        self.loader.ready.connect(self._on_loaded)
        self.loader.failed.connect(self._on_load_failed)
        self.bg.done.connect(self._bg_done)
        self.bg.failed.connect(self._bg_failed)

    def _snap_queue_width(self, *_args) -> None:
        """Let the queue be a whole number of pages wide, and nothing else.

        Dragging the divider to an arbitrary pixel leaves a strip the grid
        cannot use - the pages do not grow into it and nothing else can go
        there - so the divider settles on the nearest column boundary instead.
        """
        sizes = self.splitter.sizes()
        if len(sizes) < 2 or sizes[0] <= 0:
            return
        want = self.queue.snapped_width(sizes[0])
        delta = want - sizes[0]
        if not delta:
            return
        # take the difference from the canvas, which is the elastic one
        sizes[0] = want
        sizes[1] = max(120, sizes[1] - delta)
        self.splitter.setSizes(sizes)

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.canvas and ev.type() == QEvent.Resize:
            self.notice.reposition()
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------ busy guard
    def _claim(self, name: str, message: str = "") -> bool:
        """Take the busy lock, or refuse politely.

        Everything slow runs on a worker thread, so the window stays live while
        it happens.  What must not happen is a second slow operation starting
        on top of the first - that is how the app used to wedge.
        """
        if self._busy:
            self.statusBar().showMessage(
                f"Busy: {sorted(self._busy)[0]}. Give it a moment.", 2500)
            return False
        self._busy.add(name)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        self.throbber.start()
        if message:
            self.statusBar().showMessage(message)
        return True

    def _release(self, name: str) -> None:
        if name in self._busy:
            self._busy.discard(name)
            QApplication.restoreOverrideCursor()
        if not self._busy:
            self.throbber.stop()

    def _bg_done(self, token: str, result) -> None:
        kind, _, arg = token.partition(":")
        try:
            handler = getattr(self, f"_done_{kind}", None)
            if handler is not None:
                handler(arg, result)
        except Exception as exc:                           # noqa: BLE001
            # A handler that dies here used to leave the import modal on screen
            # for ever: it is the handler that asks for the pixels, so nothing
            # would ever report in and the dialog has no close button.  Qt would
            # print the traceback and carry on, with the app frozen.
            self.statusBar().showMessage(f"{kind} failed: {exc}", 8000)
            if self._load_dialog is not None:
                self._load_dialog.finish()
            raise
        finally:
            self._release(kind)

    def _bg_failed(self, token: str, message: str) -> None:
        kind = token.split(":", 1)[0]
        self._release(kind)
        self.notice.dismiss()
        # An import runs behind a modal with no way out, so a step of it dying
        # must let go of the window rather than leaving the user staring at a
        # dialog that will never finish.
        if kind == "probe" and self._load_dialog is not None:
            self._load_dialog.finish()
        self.statusBar().showMessage(f"{kind} failed: {message}", 6000)

    # --------------------------------------------------------------- history
    def _begin_history(self, label: str) -> None:
        self.history.begin(label)
        self._history_timer.start()

    def _end_history(self) -> None:
        self._history_timer.stop()
        self.history.commit()

    def _history_changed(self) -> None:
        self.a_undo.setEnabled(self.history.can_undo)
        self.a_redo.setEnabled(self.history.can_redo)
        u, r = self.history.undo_label(), self.history.redo_label()
        self.a_undo.setToolTip(f"Undo {u}   (Ctrl+Z)" if u else "Nothing to undo")
        self.a_redo.setToolTip(f"Redo {r}   (Ctrl+Y)" if r else "Nothing to redo")

    def undo(self) -> None:
        self._history_timer.stop()
        self.history.commit()
        label = self.history.undo()
        if label:
            self._after_history(f"Undid: {label}")

    def redo(self) -> None:
        label = self.history.redo()
        if label:
            self._after_history(f"Redid: {label}")

    def _after_history(self, message: str) -> None:
        self._dirty = True
        uid = self._current.uid if self._current else None
        self.a_link.setChecked(self.project.link_crop)
        self.inspector.set_link(self.project.link_crop)
        # Undo changes page state, never the pixels on disk, so the decoded
        # thumbnails stay valid.  Discarding them here made every Ctrl+Z re-read
        # every thumbnail from the cache on the interface thread.
        self.queue.drop_stale_icons()
        self.queue.rebuild()
        self._rearm_first_crop()
        page = self.project.by_uid(uid) if uid else None
        if page is None and self.project.pages:
            page = self.project.pages[0]
        if page is not None:
            self.queue.select(page.uid)
            self._current = page
            if self._proxy_raw is not None and self.canvas.edit is page:
                self._reorient()          # undo may have changed the orientation
                self.canvas.set_page(page, self._proxy)
                self.inspector.set_page(page)
            else:
                self.show_page(page.uid)
        else:
            self._current = None
            self.canvas.set_page(None, None)
            self.inspector.set_page(None)
        self.statusBar().showMessage(message, 3000)

    # ------------------------------------------------------------ page load
    def show_page(self, uid: str) -> None:
        page = self.project.by_uid(uid)
        if page is None:
            return

        if self._spread_pick and self._spread_pick != uid:
            partner = self.project.by_uid(self._spread_pick)
            self._spread_pick = None
            if partner is not None:
                self._launch_spread(partner, page)
                return

        self._current = page
        self._detail_loaded = False
        if not page.src_w:
            try:
                meta = iio.probe(page.path)
                page.src_w, page.src_h = meta["width"], meta["height"]
                page.dpi = meta["dpi"]
                page.channels = meta["channels"]
                page.dtype = meta["dtype"]
            except Exception:                          # noqa: BLE001
                pass

        idx = self.project.index_of(uid) + 1
        self.lbl_progress.setText(f"Page {idx} of {len(self.project.pages)}")
        self.inspector.set_page(page)

        # Cached proxies come back in about 50 ms, so read those inline; a page
        # that still needs decoding goes to a worker instead of freezing the UI.
        if page.join:
            arr = self._joined_proxy(page)
        else:
            arr = proxycache.load(page.path, "proxy", build_if_missing=False)
        if arr is not None:
            self.notice.dismiss()
            self._display(page, arr)
        else:
            self._proxy_raw = None
            self._proxy = None
            self.canvas.set_page(page, None)
            self.canvas.set_message("")
            self.notice.show_notice(
                f"Preparing page {idx} of {len(self.project.pages)}",
                f"{page.name} has not been read yet. This happens once per "
                f"scan; afterwards it opens instantly.\n\n"
                f"The window stays usable — you can keep browsing the queue.",
                spinning=True)
            if page.join:
                for part in page.parts():
                    self.loader.request(part.path, "proxy")
            else:
                self.loader.request(page.path, "proxy")
        self._prefetch_neighbours(uid)

    def _joined_proxy(self, page: PageEdit) -> Optional[np.ndarray]:
        """Assemble a preview of a joined page from the parts' own proxies.

        Building it here rather than caching a composite keeps the cache keyed
        by source file: adjust one half's crop and the preview simply rebuilds,
        with no stale assembled copy to invalidate.  At proxy size it is a
        couple of array copies.
        """
        parts = page.parts()
        arrs, scales = [], []
        for part in parts:
            a = proxycache.load(part.path, "proxy", build_if_missing=False)
            if a is None:
                return None
            arrs.append(a)
            scales.append(a.shape[1] / float(part.src_w or a.shape[1]))
        W, H = mdl.join_size(page.join)
        scale = min(1.0, proxycache.PROXY / float(max(W, H) or 1))
        try:
            return rnd.build_join(page, arrs, scale=scale, part_scales=scales,
                                  interp="linear")
        except Exception:                                  # noqa: BLE001
            return None

    def _reorient(self) -> None:
        """Re-derive the displayed proxy from the decoded one.

        The 90-degree rotations and flips have to be applied to the preview,
        not just at export.  They were not, which is why those buttons looked
        dead: the pixmap never changed, and because base_size() *does* swap
        width and height for a quadrant turn while the pixmap did not, the
        proxy scale went wrong with it and took the crop numbers along.

        Deriving this from the decoded array rather than re-reading means a
        90-degree turn costs a memory copy, not a disk decode.
        """
        e = self._current
        if e is None or self._proxy_raw is None:
            self._proxy = None
            return
        self._proxy = rnd.apply_orientation(self._proxy_raw, e.quadrant,
                                            e.flip_h, e.flip_v)

    def _display(self, page: PageEdit, arr: np.ndarray) -> None:
        self._proxy_raw = arr
        self._reorient()
        self.canvas.set_page(page, self._proxy)
        self.inspector.set_page(page)
        self.inspector.set_blur(-1, None)
        self._maybe_arm_first_crop(page)

    def _maybe_arm_first_crop(self, page: PageEdit) -> None:
        """A new project opens ready to crop.

        Cropping is the first thing you do to a fresh set of scans, and with
        the crop shared across the project it is a single action for the whole
        document - so the tool arms itself once, then releases as usual.
        """
        if not self._first_crop_pending or page.crop is not None:
            return
        if any(p.crop for p in self.project.pages):
            self._first_crop_pending = False
            return
        self._first_crop_pending = False
        self._rearm_first_crop()
        self.set_tool(Tool.CROP)
        self.statusBar().showMessage(
            "Draw the crop for this document — it will be applied to all "
            f"{len(self.project.pages)} pages", 12000)

    def _on_loaded(self, path: str, tier: str, arr) -> None:
        if self._current is None or arr is None:
            return
        if self._current.join:
            # a part of the page just arrived; the composite can only be built
            # once every part is in the cache, so ask again and wait if not
            if path not in {p.path for p in self._current.parts()}:
                return
            if tier != "proxy":
                return
            composite = self._joined_proxy(self._current)
            if composite is not None:
                self.notice.dismiss()
                self._display(self._current, composite)
            return
        if self._current.path != path:
            return
        if tier == "proxy":
            self.notice.dismiss()
            self._display(self._current, arr)
            self.statusBar().showMessage(f"{self._current.name} ready", 1500)
        elif tier == "detail":
            zoom = self.canvas.view_scale() / (self.canvas._scale or 1.0)
            if zoom >= 0.38:
                e = self._current
                self.canvas.set_detail_pixmap(
                    rnd.apply_orientation(arr, e.quadrant, e.flip_h, e.flip_v))

    def _on_load_failed(self, path: str, message: str) -> None:
        if self._current is not None and self._current.path == path:
            self.canvas.set_message(f"Could not read {os.path.basename(path)}\n{message}")
        self.statusBar().showMessage(f"{os.path.basename(path)}: {message}", 6000)

    def _prefetch_neighbours(self, uid: str, span: int = 3) -> None:
        i = self.project.index_of(uid)
        for j in range(i - 1, i + span + 1):
            if 0 <= j < len(self.project.pages) and j != i:
                page = self.project.pages[j]
                self.prefetch.request(page.path)
                for part in page.parts():
                    self.prefetch.request(part.path)

    def _maybe_load_detail(self) -> None:
        if self._current is None or self._detail_loaded or self._proxy is None:
            return
        if self._current.join:
            # the detail tier is one source file, and this page is several
            # assembled together: swapping it in would replace the whole
            # composite with one of its halves
            return
        zoom = self.canvas.view_scale() / (self.canvas._scale or 1.0)
        if zoom < 0.38:
            return
        self._detail_loaded = True
        self.loader.request(self._current.path, "detail")

    def _cursor_moved(self, x: float, y: float) -> None:
        if self._current is None:
            return
        dpi = self._current.dpi or 600.0
        self.lbl_pos.setText(f"{x:.0f}, {y:.0f} px   ({x / dpi:.2f}, {y / dpi:.2f} in)")

    # -------------------------------------------------------------- editing
    def _commit_crop(self) -> None:
        """Propagate the current page's crop if the project is linked."""
        if self._current is None:
            return
        n = self.project.apply_linked_crop(self._current)
        if n:
            self._queue_timer.start()
        # A page exempt from the shared crop cannot be the crop that shapes the
        # document: cropping a joined or spread page propagates nothing, so
        # releasing the link there would leave the rest of the pages uncropped
        # and the link switched off.
        if (getattr(self, "_first_crop_armed", False) and self._current.crop
                and self._current.crop_mode != "locked"):
            self._release_project_crop(n)

    def _rearm_first_crop(self) -> None:
        """Work out whether the next crop is the one that shapes the document.

        Derived from the project rather than remembered, because a remembered
        flag does not survive undo: cropping, undoing and cropping again left
        the shared crop switched on for the rest of the session, which is the
        exact failure the release exists to prevent.
        """
        self._first_crop_armed = (self.project.link_crop
                                  and not any(p.crop for p in self.project.pages))

    def _release_project_crop(self, applied: int) -> None:
        """Turn the project-wide crop off once it has done its job.

        The shared crop exists to answer one question - what shape is this
        document - and it answers it the first time you draw a crop.  Leaving
        it switched on after that is what made careful per-page work disappear:
        every later nudge on any page was quietly copied over all the others.
        So it releases itself, exactly like the crop tool does, and can be
        switched back on from the tool bar when it is genuinely wanted.
        """
        self._first_crop_armed = False
        if not self.project.link_crop:
            return
        self.project.link_crop = False
        self.a_link.setChecked(False)
        self.inspector.set_link(False)
        self._dirty = True
        total = sum(1 for p in self.project.pages if p.crop)
        self.statusBar().showMessage(
            f"Crop applied to {total} pages. The project-wide crop is now "
            f"off, so further adjustments only affect the page you are on.",
            12000)

    # ------------------------------------------------------- cache warming
    def _start_caching(self, pages: list) -> None:
        """Ask for every page's pixels.  Runs inside the import modal.

        Editing a queue that is still half read is how the app used to get
        wedged, so it is not merely discouraged: the window is genuinely
        unavailable until this finishes.  The dialog has no Cancel, no close
        button and ignores Escape - it goes away when the work is done.

        Being modal is also what makes it safe.  exec() runs the event loop
        with input blocked, so the dialog animates and the workers report in
        while nothing can reach the half-built project underneath - the one
        promise a bare processEvents() could never make.
        """
        dlg = self._load_dialog
        # A page whose proxy is already cached needs nothing; tell the dialog
        # so, rather than waiting for a notification about work that will not
        # happen.  Duplicates matter too - the halves of a cut scan share a
        # file, and the dialog counts one completion per path.
        seen = set()
        todo = []
        for p in pages:
            key = os.path.normcase(os.path.abspath(p.path))
            if key in seen:
                continue
            seen.add(key)
            if proxycache.has(p.path, "proxy"):
                if dlg is not None:
                    dlg.mark_done(p.path, True)
            else:
                todo.append(p)

        if dlg is not None:
            dlg.set_phase(f"Reading {len(todo)} of {len(seen)} pages"
                          if todo else "Everything was already cached")

        # One request per page, for everything that page needs.  Asking for the
        # thumbnail and then the proxy separately used to mean two decodes of
        # the same 140 MP file - the single biggest reason importing a folder
        # took as long as it did.
        for e in pages:
            self.prefetch.request(e.path)
            # a joined page is several files; all of them have to be warm
            for part in e.parts():
                self.prefetch.request(part.path)
        if dlg is not None and not todo:
            dlg.finish()

    def _after_caching(self) -> None:
        if self._current is None and self.project.pages:
            self.queue.select(self.project.pages[0].uid)

    def _cache_started(self, path: str, tiers) -> None:
        # only the proxy job counts; a thumbnail is separate, much quicker work
        if self._load_dialog is not None and "proxy" in tiers:
            self._load_dialog.mark_start(path)

    def _cache_progress(self, path: str, fraction: float) -> None:
        if self._load_dialog is not None:
            self._load_dialog.mark_progress(path, fraction)

    def _cache_finished(self, path: str, tiers, ok: bool) -> None:
        if self._load_dialog is not None and "proxy" in tiers:
            self._load_dialog.mark_done(path, ok)

    def _refresh_queue_now(self) -> None:
        self.queue.forget_thumbs()
        self.queue.refresh_all_labels()

    def _blur_selected(self, index: int) -> None:
        blur = None
        if self._current is not None and 0 <= index < len(self._current.blurs):
            blur = self._current.blurs[index]
        self.inspector.set_blur(index, blur)

    def _canvas_edited(self) -> None:
        self._dirty = True
        self._commit_crop()
        self.inspector.refresh()
        if self._current:
            self.queue.refresh_item(self._current.uid)

    def _set_angle(self, val: float) -> None:
        self._begin_history("Rotate")
        self.canvas.set_angle(val)
        self._dirty = True

    def _set_crop(self, x, y, w, h) -> None:
        if self._current is None:
            return
        self._begin_history("Set crop")
        self._current.crop = [x, y, w, h]
        self._commit_crop()
        self.canvas.viewport().update()
        self.inspector.refresh_output()
        self._dirty = True

    def _set_orientation(self, quadrant: int, fh: bool, fv: bool) -> None:
        """Turn or flip the page, bringing the crop and blurs along with it.

        Both are stored in the rotated frame, so a quadrant turn rotates them
        inside the old canvas and a flip mirrors them inside the new one.  The
        mirror is exact at zero skew and out by well under a pixel at the
        fractions of a degree this app deals in.
        """
        e = self._current
        if e is None:
            return
        if (quadrant, fh, fv) == (e.quadrant, e.flip_h, e.flip_v):
            return
        self.history.capture("Orientation")
        from ..geometry import transform_rect_quadrant, rotated_size

        steps = (quadrant - e.quadrant) % 4
        if steps:
            canvas = rotated_size(*e.base_size(), e.angle)
            if e.crop:
                e.crop = list(
                    transform_rect_quadrant(Rect(*e.crop), canvas, steps).as_tuple())
            for b in e.blurs:
                r = transform_rect_quadrant(Rect(b[0], b[1], b[2], b[3]),
                                            canvas, steps)
                b[0], b[1], b[2], b[3] = r.x, r.y, r.w, r.h
        e.quadrant = quadrant

        # flips act on the already-turned page, so measure the canvas again
        W, H = rotated_size(*e.base_size(), e.angle)
        if fh != e.flip_h:
            if e.crop:
                e.crop[0] = W - (e.crop[0] + e.crop[2])
            for b in e.blurs:
                b[0] = W - (b[0] + b[2])
        if fv != e.flip_v:
            if e.crop:
                e.crop[1] = H - (e.crop[1] + e.crop[3])
            for b in e.blurs:
                b[1] = H - (b[1] + b[3])
        e.flip_h, e.flip_v = fh, fv

        self._commit_crop()
        self._reorient()
        self._reload_current()
        turn = {0: "", 1: "90° right", 2: "180°", 3: "90° left"}[quadrant]
        self.statusBar().showMessage(
            "Orientation: " + (turn or "upright")
            + (" · flipped ⇄" if fh else "") + (" · flipped ⇅" if fv else ""), 3000)

    def _reload_current(self) -> None:
        if self._current is None:
            return
        self.canvas.set_page(self._current, self._proxy)
        self.inspector.set_page(self._current)
        self._dirty = True

    def _set_done(self, on: bool) -> None:
        if self._current and self._current.done != on:
            self._current.done = on
            self.queue.refresh_item(self._current.uid)
            self._dirty = True

    def _set_link(self, on: bool) -> None:
        if self.project.link_crop == on:
            return
        self.history.capture("Link crops" if on else "Unlink crops")
        self.project.link_crop = on
        self.a_link.setChecked(on)
        # switching the link back on by hand is a deliberate act; it must not
        # then switch itself off again on the next crop
        self._rearm_first_crop()
        if on:
            self._commit_crop()
        self._dirty = True
        self.statusBar().showMessage(
            "Every page now shares one crop" if on
            else "Pages can be cropped independently", 4000)

    def toggle_link(self, on: bool) -> None:
        self._set_link(on)
        self.inspector.set_link(on)

    def _set_crop_mode(self, mode: str) -> None:
        if self._current is None or self._current.crop_mode == mode:
            return
        self.history.capture("Crop link for this page")
        self._current.crop_mode = mode
        self._dirty = True
        self.queue.refresh_item(self._current.uid)
        self.statusBar().showMessage(
            {"linked": "This page follows the shared crop",
             "own_position": "This page keeps its own position, shared size",
             "locked": "This page's crop is locked — nothing will overwrite it",
             }.get(mode, ""), 4000)

    def set_tool(self, tool: Tool) -> None:
        self.canvas.tool = tool
        self.a_tool_cursor.setChecked(tool == Tool.CURSOR)
        self.a_tool_crop.setChecked(tool == Tool.CROP)
        self.a_tool_line.setChecked(tool == Tool.ALIGN)
        self.a_tool_blur.setChecked(tool == Tool.BLUR)
        self.a_tool_cut.setChecked(tool == Tool.CUT)
        if tool != Tool.CUT:
            self.canvas._cut = None
        if tool == Tool.BLUR:
            self.statusBar().showMessage(
                "Drag out a blur, or click to drop one — then drag it into place",
                6000)
        elif tool == Tool.CROP:
            self.statusBar().showMessage(
                "Drag to draw one crop — the tool releases afterwards", 6000)
        elif tool == Tool.ALIGN:
            self.statusBar().showMessage(
                "Drag out the straightedge — it stays put, and you turn the "
                "page onto it with [ and ]. Alt keeps a free angle.", 8000)
        elif tool == Tool.CUT:
            self.statusBar().showMessage(
                "Click where the scan should be cut — Shift cuts across "
                "instead of down", 8000)

    def escape(self) -> None:
        if getattr(self, "_mode", None) is not None:
            self.close_mode()
            return
        if self._spread_pick:
            self._spread_pick = None
            self.notice.dismiss()
            if self._current is not None and self._proxy is not None:
                self.canvas.set_page(self._current, self._proxy)
            self.statusBar().showMessage("Spread selection cancelled", 3000)
            return
        if self.canvas.tool != Tool.CURSOR:
            self.set_tool(Tool.CURSOR)

    def toggle_grid(self, on: bool) -> None:
        self.canvas.show_grid = on
        self.canvas.viewport().update()

    def toggle_thirds(self, on: bool) -> None:
        self.canvas.show_thirds = on
        self.canvas.viewport().update()

    def toggle_dim(self, on: bool) -> None:
        self.canvas.dim_outside = on
        self.canvas.viewport().update()

    def clear_crop(self) -> None:
        if self._current is None:
            return
        self.history.capture("Clear crop")
        self.canvas.reset_crop()
        if self.project.link_crop:
            for p in self.project.pages:
                p.crop = None
            self.queue.forget_thumbs()
            self.queue.refresh_all_labels()
        self._dirty = True
        # back to an uncropped document, so the next crop is the first one again
        self._rearm_first_crop()

    def select_all_crop(self) -> None:
        self.history.capture("Whole page")
        self.canvas.select_all()

    # ----------------------------------------------------------- automations
    # These all run OpenCV over the proxy.  It only takes a fraction of a
    # second, but a fraction of a second of frozen window on every page adds up
    # to something that feels broken, so they go to a worker thread like
    # everything else and report back by signal.
    def auto_deskew(self) -> None:
        if self._current is None or self._proxy is None:
            return
        if not self._claim("deskew", "Measuring skew…"):
            return
        self.bg.run(f"deskew:{self._current.uid}", autodetect.detect_skew, self._proxy)

    def _done_deskew(self, uid: str, angle: float) -> None:
        page = self.project.by_uid(uid)
        if page is None or self._current is not page:
            return
        self.history.capture("Auto-deskew")
        self.canvas.set_angle(round(page.angle + angle, 3))
        self.inspector.refresh()
        self.statusBar().showMessage(f"Deskewed by {angle:+.3f}°", 4000)

    def fit_to_page(self) -> None:
        if self._current is None or self._proxy is None:
            return
        if not self._claim("fitpage", "Finding the sheet…"):
            return
        self.bg.run(f"fitpage:{self._current.uid}",
                    autodetect.detect_page_rotated, self._proxy, self._current.angle)

    def _done_fitpage(self, uid: str, rect) -> None:
        page = self.project.by_uid(uid)
        if page is None or self._current is not page:
            return
        if rect is None:
            self.statusBar().showMessage(
                "No page edge found — the sheet may already fill the scan", 5000)
            return
        self.history.capture("Fit to page edges")
        s = self.canvas._scale or 1.0
        page.crop = list(rect.scaled(1.0 / s).as_tuple())
        self._commit_crop()
        self.canvas.viewport().update()
        self.inspector.refresh()
        self._dirty = True
        note = " and applied to every page" if self.project.link_crop else ""
        self.statusBar().showMessage(f"Crop set to the detected page edges{note}", 4000)

    @staticmethod
    def _auto_both_work(proxy):
        """Straighten and crop in one pass, entirely off the GUI thread."""
        found = autodetect.detect_page(proxy)
        angle = found["angle"] if found else 0.0
        if abs(angle) < 0.02:
            angle = autodetect.detect_skew(proxy)
        rect = autodetect.detect_page_rotated(proxy, angle)
        return angle, rect

    def auto_both(self) -> None:
        if self._current is None or self._proxy is None:
            return
        if not self._claim("auto", "Straightening and cropping…"):
            return
        self.bg.run(f"auto:{self._current.uid}", self._auto_both_work, self._proxy)

    def _done_auto(self, uid: str, result) -> None:
        page = self.project.by_uid(uid)
        if page is None or self._current is not page:
            return
        angle, rect = result
        self.history.capture("Auto straighten and crop")
        self.canvas.set_angle(round(angle, 3))
        if rect is not None:
            s = self.canvas._scale or 1.0
            page.crop = list(rect.scaled(1.0 / s).as_tuple())
            self._commit_crop()
        self.canvas.viewport().update()
        self.inspector.refresh()
        self._dirty = True
        self.statusBar().showMessage(
            f"Straightened by {angle:+.3f}° and cropped to the sheet", 4000)

    def reset_page(self) -> None:
        e = self._current
        if e is None:
            return
        self.history.capture("Reset page")
        e.angle = 0.0
        e.crop = None
        e.quadrant = 0
        e.flip_h = e.flip_v = False
        self._reload_current()

    def copy_previous(self) -> None:
        e = self._current
        if e is None:
            return
        i = self.project.index_of(e.uid)
        if i <= 0:
            self.statusBar().showMessage("This is the first page", 3000)
            return
        self.history.capture("Copy framing")
        e.copy_adjustments_from(self.project.pages[i - 1])
        self._reload_current()
        self.queue.refresh_item(e.uid)
        self.statusBar().showMessage("Framing copied from the previous page", 3000)

    def _apply_framing(self, targets: list, label: str) -> None:
        e = self._current
        if e is None or not targets:
            return
        self.history.capture(label)
        n = 0
        for t in targets:
            if t.uid == e.uid:
                continue
            t.copy_adjustments_from(e)
            n += 1
        self.queue.forget_thumbs()
        self.queue.refresh_all_labels()
        self._dirty = True
        self.statusBar().showMessage(f"Framing applied to {n} pages", 4000)

    def apply_to_document(self) -> None:
        """Every page in the project is a page of the same document."""
        self._apply_framing(list(self.project.pages), "Apply framing to all pages")

    def apply_to_selection(self) -> None:
        uids = set(self.queue.selected_uids())
        self._apply_framing([p for p in self.project.pages if p.uid in uids],
                            "Apply framing to selection")

    def set_document_name(self) -> None:
        """One name for the whole set; it is what every exported file is called."""
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Document name",
            "Name for this document.\nEvery page and the PDF are named from it:",
            text=self.project.suggested_name())
        if not ok:
            return
        self.history.capture("Document name")
        self.project.document_name = name.strip()
        self.export_cfg.document = name.strip()
        self._dirty = True
        self._update_title()

    # --------------------------------------------------------------- navigate
    def step(self, delta: int) -> None:
        uid = self.queue.step(delta)
        if uid is None:
            self.statusBar().showMessage("End of the queue", 2500)

    def done_and_next(self) -> None:
        """Mark every selected page done, then move on.

        Marking only the current page meant a multi-selection silently did the
        wrong thing - you would select ten pages, press Enter, and nine of them
        stayed untouched.
        """
        uids = self.queue.selected_uids()
        if self._current and self._current.uid not in uids:
            uids.append(self._current.uid)
        pages = [p for p in (self.project.by_uid(u) for u in uids) if p]
        if not pages:
            return
        self.history.capture(f"Mark {len(pages)} done" if len(pages) > 1
                             else "Mark done")
        for p in pages:
            p.done = True
            self.queue.refresh_item(p.uid)
        if self._current is not None and self._current.done:
            self.inspector.chk_done.setChecked(True)
        self._dirty = True
        if len(pages) > 1:
            self.statusBar().showMessage(f"Marked {len(pages)} pages done", 3000)
        self.step(1)

    def toggle_done_selection(self) -> None:
        """Flip done on the selection - the inverse of the Enter key."""
        uids = self.queue.selected_uids()
        pages = [p for p in (self.project.by_uid(u) for u in uids) if p]
        if not pages:
            return
        target = not all(p.done for p in pages)
        self.history.capture("Mark done" if target else "Mark not done")
        for p in pages:
            p.done = target
            self.queue.refresh_item(p.uid)
        if self._current is not None:
            self.inspector.chk_done.setChecked(self._current.done)
        self._dirty = True
        self.statusBar().showMessage(
            f"{len(pages)} pages marked {'done' if target else 'not done'}", 3000)

    # ---------------------------------------------------------------- spread
    def open_spread(self) -> None:
        uids = self.queue.selected_uids()
        if len(uids) >= 2:
            order = sorted(uids[:2], key=self.project.index_of)
            self._launch_spread(self.project.by_uid(order[0]),
                                self.project.by_uid(order[1]))
            return
        if not uids:
            self.statusBar().showMessage("Select a page first", 3000)
            return
        self._spread_pick = uids[0]
        page = self.project.by_uid(uids[0])
        self.notice.show_notice(
            "Pick the facing page",
            f"“{page.name}” is the first half of the spread.\n\n"
            f"Click the page it faces in the queue on the left, and the two "
            f"will open side by side for alignment.",
            spinning=False,
            actions=[("Cancel", self.escape)])
        self.statusBar().showMessage(
            f"Spread: click the page that faces “{page.name}”", 20000)

    def _launch_spread(self, left: PageEdit, right: PageEdit,
                       join: bool = False) -> None:
        if left is None or right is None:
            return
        if not self._claim("spread", "Loading both pages…"):
            return
        self._spread_join = join
        self.notice.show_notice("Opening the spread",
                                "Reading both pages…", spinning=True)
        # Either proxy may still need a full decode of a 140 MP TIFF.  Doing
        # that inline was the single worst freeze in the app.
        self.bg.run(f"spread:{left.uid}|{right.uid}", self._spread_work,
                    left.path, right.path)

    @staticmethod
    def _spread_work(left_path: str, right_path: str):
        return (proxycache.load(left_path, "proxy"),
                proxycache.load(right_path, "proxy"))

    def _done_spread(self, arg: str, result) -> None:
        luid, _, ruid = arg.partition("|")
        left, right = self.project.by_uid(luid), self.project.by_uid(ruid)
        lp, rp = result
        if left is None or right is None or lp is None or rp is None:
            QMessageBox.warning(self, "Align as spread", "Could not load both pages.")
            return
        # the spread view measures against base_size(), so it needs the pages
        # the same way round the canvas shows them
        lp = rnd.apply_orientation(lp, left.quadrant, left.flip_h, left.flip_v)
        rp = rnd.apply_orientation(rp, right.quadrant, right.flip_h, right.flip_v)
        self.notice.dismiss()
        self.history.capture("Align spread")
        self.open_aligner(SpreadPane(left, right, lp, rp, self,
                                     join=getattr(self, "_spread_join", False)),
                          left, right)
        self.statusBar().clearMessage()

    # ------------------------------------------------- modes in the work area
    def open_aligner(self, pane, left: PageEdit, right: PageEdit) -> None:
        """Put the aligner where the canvas is, rather than over the top of it."""
        self.close_mode()
        pane.applied.connect(lambda: self._after_spread(left, right))
        pane.joinRequested.connect(lambda gap: self._do_join(left, right, gap))
        pane.finished.connect(self.close_mode)
        self._mode = pane
        self.center.addWidget(pane)
        self.center.setCurrentWidget(pane)
        pane.setFocus()
        self.statusBar().showMessage(
            "Aligning — the queue and the inspector stay where they are; "
            "Esc leaves the aligner", 8000)

    def close_mode(self) -> None:
        """Back to the editing canvas."""
        pane = getattr(self, "_mode", None)
        if pane is None:
            return
        self._mode = None
        self.center.setCurrentWidget(self.canvas)
        self.center.removeWidget(pane)
        pane.deleteLater()
        self.canvas.setFocus()

    def _after_spread(self, left: PageEdit, right: PageEdit) -> None:
        self._dirty = True
        self.queue.forget_thumbs([left.path, right.path])
        for p in (left, right):
            self.queue.refresh_item(p.uid)
        if self._current and self._current.uid in (left.uid, right.uid):
            self.inspector.set_page(self._current)
            self._reload_current()
        if left.crop_mode == "locked" and self.project.link_crop:
            self.statusBar().showMessage(
                "Spread saved. Both pages are now locked, so the shared crop "
                "will not overwrite their alignment.", 8000)

    # ------------------------------------------------------------- cut / join
    def _do_cut(self, axis: str, fractions) -> None:
        """Turn one scan into several pages along the given cut positions.

        Nothing is written: each piece is the same file with a narrower crop,
        so a cut costs no disk space and is undone by undo like anything else.
        """
        page = self._current
        if page is None:
            return
        fractions = list(fractions or [])
        if not fractions:
            return
        self.history.capture("Cut page")
        pieces = page.cut(fractions, axis)
        if len(pieces) < 2:
            self.statusBar().showMessage("Nothing to cut there", 3000)
            return
        self.project.replace(page.uid, pieces)
        self._dirty = True
        self.queue.forget_thumbs([page.path])
        self.queue.rebuild()
        self.queue.select(pieces[0].uid)
        self.show_page(pieces[0].uid)
        sizes = " and ".join(f"{w}×{h}" for w, h in
                             (p.output_size() for p in pieces))
        self.statusBar().showMessage(
            f"Cut into {len(pieces)} pages — {sizes}. Each keeps its own crop, "
            f"so the shared crop will not flatten them.", 8000)

    def join_pages(self) -> None:
        """Align two scans and stitch them into a single page."""
        uids = self.queue.selected_uids()
        if len(uids) < 2:
            self.statusBar().showMessage(
                "Select the two scans to join — they open side by side first, "
                "so you can line them up before they become one page", 6000)
            return
        order = sorted(uids[:2], key=self.project.index_of)
        self._launch_spread(self.project.by_uid(order[0]),
                            self.project.by_uid(order[1]), join=True)

    def _do_join(self, left: PageEdit, right: PageEdit, gap: int) -> None:
        page = PageEdit.joined([left, right], axis="h", gap=gap)
        self.history.capture("Join pages")
        i = self.project.index_of(left.uid)
        self.project.pages = [p for p in self.project.pages
                              if p.uid not in (left.uid, right.uid)]
        self.project.pages.insert(max(0, i), page)
        self._dirty = True
        self.queue.rebuild()
        self.queue.select(page.uid)
        self.show_page(page.uid)
        w, h = page.output_size()
        self.statusBar().showMessage(
            f"Joined into one {w}×{h} page. Crop it to trim the seam; "
            f"“Split a joined page apart” puts the two scans back.", 9000)

    def unjoin_page(self) -> None:
        # the queue's own menu may be acting on a page that is not the one
        # open in the canvas
        page = next((p for p in (self.project.by_uid(u)
                                 for u in self.queue.selected_uids())
                     if p is not None and p.join), self._current)
        if page is None or not page.join:
            self.statusBar().showMessage("This page was not joined from others",
                                         3000)
            return
        self.history.capture("Split joined page")
        parts = page.unjoin()
        self.project.replace(page.uid, parts)
        self._dirty = True
        self.queue.rebuild()
        self.queue.select(parts[0].uid)
        self.show_page(parts[0].uid)
        self.statusBar().showMessage(
            f"Back to {len(parts)} separate pages, each with the alignment it "
            f"had", 6000)

    def open_book(self) -> None:
        """Read the document through, to check it before it goes out."""
        if not self.project.pages:
            self.statusBar().showMessage("Nothing to preview yet", 3000)
            return
        book = getattr(self, "_book", None)
        if book is not None and book.isVisible():
            book.reload()
            book.raise_()
            book.activateWindow()
            return
        self._book = BookWindow(self.project, self)
        self._book.show()
        self.statusBar().showMessage(
            "Reading view — arrows or click the page edges to turn, "
            "Ctrl+wheel to zoom", 8000)

    def match_sizes(self) -> None:
        from .size_match_dialog import SizeMatchDialog

        if len(self.project.pages) < 2:
            self.statusBar().showMessage("Nothing to match yet", 3000)
            return
        dlg = SizeMatchDialog(self.project, self)
        if dlg.exec() != SizeMatchDialog.Accepted:
            return
        uids, size = dlg.result_target()
        if not uids:
            return
        self.history.capture("Match crop sizes")
        n = self.project.match_sizes(uids, size)
        self._dirty = True
        self.queue.rebuild()
        if self._current is not None:
            self._reload_current()
            self.inspector.set_page(self._current)
        self.statusBar().showMessage(
            f"{n} pages now export at exactly {size[0]}×{size[1]}", 6000)

    # ------------------------------------------------------------- file ops
    def open_folder(self) -> None:
        start = self.project.last_folder or self.settings.value("last_folder", "")
        d = QFileDialog.getExistingDirectory(self, "Choose a folder of scans", start)
        if d:
            self.add_paths([d])

    def add_files(self) -> None:
        start = self.project.last_folder or ""
        exts = " ".join("*" + e for e in sorted(iio.RASTER_EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add scans", start,
                                                f"Images ({exts});;All files (*)")
        self.add_paths(files)

    def _adopt_working_folder(self, dropped: list) -> None:
        """A dropped folder (or a file's folder) becomes the working folder.

        This used to run only when no folder had been adopted yet, which is why
        the document name came out right sometimes and not others: the second
        folder you opened never replaced the first.
        """
        for p in dropped:
            folder = p if os.path.isdir(p) else os.path.dirname(p)
            if not folder:
                continue
            self.project.last_folder = folder
            self.settings.setValue("last_folder", folder)
            name = os.path.basename(os.path.normpath(folder))
            if not self.project.document_name:
                self.project.document_name = name
                self.export_cfg.document = name
            # alongside the scans, not buried inside them
            if not self.export_cfg.out_dir:
                self.export_cfg.out_dir = self.project.default_export_dir()
            self._update_title()
            self.statusBar().showMessage(f"Working folder: {folder}", 6000)
            return

    def _update_title(self) -> None:
        name = self.project.document_name or (
            os.path.splitext(os.path.basename(self.project.path))[0]
            if self.project.path else "")
        self.setWindowTitle(f"{APP} — {name}" if name else APP)

    def new_project(self, ask: bool = True) -> bool:
        """Start over. Returns False if the user backed out."""
        if ask and self._dirty and self.project.pages:
            r = QMessageBox.question(
                self, APP, "Save the current project first?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return False
            if r == QMessageBox.Yes:
                self.save_project()
        self.project.clear()
        self.export_cfg.out_dir = ""
        self.export_cfg.document = ""
        self.history.clear()
        self._current = None
        self._proxy_raw = self._proxy = None
        self._spread_pick = None
        self._first_crop_pending = True
        self._first_crop_armed = False
        self.queue.forget_thumbs()
        self.queue.rebuild()
        self.canvas.set_page(None, None)
        self.canvas.set_message("Drop a folder of scans here, or press Ctrl+O")
        self.inspector.set_page(None)
        self.a_link.setChecked(True)
        self.inspector.set_link(True)
        self._dirty = False
        self._update_title()
        return True

    def _ask_new_or_add(self, paths: list) -> Optional[bool]:
        """True to start fresh, False to add, None to cancel."""
        folders = [p for p in paths if os.path.isdir(p)]
        what = os.path.basename(os.path.normpath(folders[0])) if folders \
            else f"{len(paths)} file(s)"
        box = QMessageBox(self)
        box.setWindowTitle("Add to this project?")
        box.setIcon(QMessageBox.Question)
        box.setText(f"“{what}” — what should happen?")
        box.setInformativeText(
            f"This project already has {len(self.project.pages)} pages"
            + (f" from “{self.project.document_name}”."
               if self.project.document_name else "."))
        b_new = box.addButton("Start a new project", QMessageBox.AcceptRole)
        b_add = box.addButton("Add to this one", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(b_new)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_new:
            return True
        if clicked is b_add:
            return False
        return None

    def add_paths(self, paths: list) -> None:
        if not paths:
            return
        if self.project.pages:
            choice = self._ask_new_or_add(paths)
            if choice is None:
                return
            if choice and not self.new_project():
                return
        self._adopt_working_folder(paths)
        files = gather_paths(paths)
        if not files:
            self.statusBar().showMessage("No readable images in that drop", 4000)
            return

        existing = self.project.paths()
        added = []
        for p in files:
            key = os.path.normcase(os.path.abspath(p))
            if key in existing:
                continue
            existing.add(key)
            edit = PageEdit(path=p)
            self.project.pages.append(edit)
            added.append(edit)
        if not added:
            self.statusBar().showMessage("Those pages are already in the queue", 3000)
            return

        self.history.capture("Add pages")
        self._dirty = True
        self._import(added)

    # ------------------------------------------------------------- importing
    def _import(self, added: list) -> None:
        """Read a set of new pages, with the window shut for the duration.

        The modal goes up *first*, before the queue is filled and before a
        single file is touched.  It used to go up last: the queue was rebuilt
        immediately, which asked for a thumbnail per page, which - because one
        decode now writes every cheap tier - started decoding the whole folder
        while the window was still live and the dialog had not appeared.  So
        the one thing the modal exists to prevent, a half-read queue you can
        click on, was happening before the modal arrived.

        Now the order is: dialog, headers, pixels, and only then the queue.
        """
        if self._load_dialog is not None:
            # An import is already running behind the modal.  A second one
            # would overwrite the first's dialog and its probe results, leaving
            # the first dialog on screen with nothing left to report to it.
            self.statusBar().showMessage(
                "Still reading the last folder — try again in a moment", 4000)
            return
        if not self._claim("probe"):
            return
        self._pending_probe = added
        self._import_pages = added
        self._load_dialog = QueueLoadDialog(
            added, self, is_ready=lambda p: proxycache.has(p, "proxy"))
        self._load_dialog.set_phase(f"Reading {len(added)} page headers…")
        self.bg.run("probe:add", self._probe_work, [e.path for e in added])
        if not self._load_dialog.is_finished:
            self._load_dialog.exec()
        self._end_import()

    def _end_import(self) -> None:
        pages = getattr(self, "_import_pages", []) or []
        dlg = self._load_dialog
        self._load_dialog = None
        self._import_pages = []
        self._pending_probe = []
        if dlg is not None:
            dlg.deleteLater()
        if self._closing:
            return                    # the window is being torn down under us

        if dlg is not None and dlg.cancelled:
            # Cancelling abandons the whole import: the pages go back out of
            # the project, and the undo entry that added them goes with them,
            # so the queue is exactly as it was before the folder was dropped.
            drop = {p.uid for p in pages}
            self.project.pages = [p for p in self.project.pages
                                  if p.uid not in drop]
            for p in pages:
                self.prefetch.forget(p.path)
            self.history.undo()
            self._dirty = bool(self.project.pages)
            self.queue.rebuild()
            self._update_title()
            self.statusBar().showMessage(
                f"Import cancelled — {len(pages)} pages were not added", 8000)
            if not self.project.pages:
                self._current = None
                self.canvas.set_page(None, None)
                self.canvas.set_message(
                    "Drop a folder of scans here, or press Ctrl+O")
                self.inspector.set_page(None)
            return

        self.queue.rebuild()
        if dlg is not None and dlg.gave_up:
            missed = len(dlg.unread_paths())
            self.statusBar().showMessage(
                f"Stopped waiting — {missed} of {len(pages)} pages were not "
                f"read. They will be read when you open them.", 15000)
        elif pages:
            self.statusBar().showMessage(f"{len(pages)} pages ready", 4000)
        self._after_caching()

    @staticmethod
    def _probe_work(paths: list) -> list:
        out = []
        for path in paths:
            try:
                out.append(iio.probe(path))
            except Exception:                          # noqa: BLE001
                out.append(None)
        return out

    def _done_probe(self, _arg: str, metas: list) -> None:
        added = getattr(self, "_pending_probe", []) or []
        for e, meta in zip(added, metas):
            if meta is None:
                e.note = "unreadable"
                continue
            e.src_w, e.src_h = meta["width"], meta["height"]
            e.dpi, e.channels, e.dtype = meta["dpi"], meta["channels"], meta["dtype"]
        self._pending_probe = []

        # Now that the sizes are known, match the decode pool to them.  Three
        # 140 MP scans in flight at once is about three gigabytes of decode
        # buffers, which on a normal machine means swapping rather than working.
        sizes = [(e.src_w * e.src_h) for e in self.project.pages if e.src_w]
        if sizes:
            big = max(sizes) / 1e6
            page = max(self.project.pages, key=lambda e: e.src_w * e.src_h)
            self.prefetch.resize(proxycache.suggested_workers(
                big, page.channels, 2 if page.dtype == "uint16" else 1))

        # if the project already has a crop, new pages inherit it
        anchor = next((p for p in self.project.pages
                       if p.crop and p not in added), None)
        if anchor is not None and self.project.link_crop:
            self.project.apply_linked_crop(anchor)
        # Every page is read now, while the window is shut, rather than the
        # first click on page 30 sitting there decoding a 140 MP TIFF with
        # nothing on screen to say why.
        self._start_caching(added)

    def remove_uids(self, uids: list) -> None:
        if not uids:
            return
        n = len(uids)
        if n > 1 and QMessageBox.question(
                self, "Remove pages",
                f"Remove {n} pages from the queue?\n\n"
                "The files on disk are not touched.") != QMessageBox.Yes:
            return
        self.history.capture(f"Remove {n} page(s)")
        drop = set(uids)
        idx = min([self.project.index_of(u) for u in uids] or [0])
        self.project.pages = [p for p in self.project.pages if p.uid not in drop]
        self.queue.rebuild()
        if self._current and self._current.uid in drop:
            self._current = None
            self._proxy_raw = None
            self._proxy = None
            self.canvas.set_page(None, None)
            self.canvas.set_message("")
            self.inspector.set_page(None)
            if self.project.pages:
                nxt = self.project.pages[min(idx, len(self.project.pages) - 1)]
                self.queue.select(nxt.uid)
        self._dirty = True
        self.statusBar().showMessage(f"Removed {n} pages from the queue", 3000)

    def remove_selected(self) -> None:
        self.remove_uids(self.queue.selected_uids())

    # ---------------------------------------------------- drag and drop
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        projects = [p for p in paths if p.lower().endswith(".sfproj")]
        if projects:
            self._open_project_file(projects[0])
        else:
            self.add_paths(paths)
        ev.acceptProposedAction()

    # ------------------------------------------------------------- project
    def save_project(self) -> None:
        if not self.project.path:
            self.save_project_as()
            return
        self.project.save(self.project.path)
        self._dirty = False
        self.statusBar().showMessage(f"Saved {os.path.basename(self.project.path)}", 3000)

    def save_project_as(self) -> None:
        start = self.project.path or os.path.join(
            self.project.last_folder or os.path.expanduser("~"), "scanforge.sfproj")
        f, _ = QFileDialog.getSaveFileName(self, "Save project", start,
                                           "ScanForge project (*.sfproj)")
        if f:
            self.project.save(f)
            self._dirty = False
            self._update_title()

    def load_project(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Open project", "",
                                           "ScanForge project (*.sfproj)")
        if f:
            self._open_project_file(f)

    def _open_project_file(self, f: str) -> None:
        try:
            proj = Project.load(f)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.critical(self, "Open project", str(exc))
            return
        self.project.pages = proj.pages
        self.project.path = proj.path
        self.project.last_folder = proj.last_folder
        self.project.export_dir = proj.export_dir
        self.project.link_crop = proj.link_crop
        self.export_cfg.out_dir = proj.export_dir or self.export_cfg.out_dir
        self.a_link.setChecked(proj.link_crop)
        self.inspector.set_link(proj.link_crop)
        self.history.clear()
        self.queue.forget_thumbs()
        self.queue.rebuild()
        # a reopened project is not a fresh one: whether the next crop is the
        # document's first is decided by what is in it, not by what the last
        # project was doing
        self._first_crop_pending = False
        self._rearm_first_crop()
        missing = [p.name for p in self.project.pages if not os.path.exists(p.path)]
        if missing:
            QMessageBox.warning(self, "Missing files",
                                f"{len(missing)} source files could not be found, "
                                f"starting with {missing[0]}")
        if self.project.pages:
            self.queue.select(self.project.pages[0].uid)
        for p in self.project.pages[:6]:
            self.prefetch.request(p.path)
        self._update_title()
        self._dirty = False

    def _autosave_project(self) -> None:
        if self._dirty and self.project.path:
            try:
                self.project.save(self.project.path)
                self._dirty = False
            except Exception:                          # noqa: BLE001
                pass

    # -------------------------------------------------------------- export
    def export(self) -> None:
        self._run_export(list(self.project.pages))

    def export_selection(self) -> None:
        uids = set(self.queue.selected_uids())
        self._run_export([p for p in self.project.pages if p.uid in uids])

    def _run_export(self, edits: list) -> None:
        edits = [e for e in edits if os.path.exists(e.path)]
        if not edits:
            QMessageBox.information(self, "Export", "Nothing to export.")
            return
        if not self.export_cfg.out_dir:
            self.export_cfg.out_dir = (self.project.default_export_dir()
                                       or os.path.expanduser("~"))
        if not self.export_cfg.document:
            self.export_cfg.document = self.project.suggested_name()
        dlg = ExportDialog(edits, self.export_cfg, self)
        if dlg.exec() != ExportDialog.Accepted:
            return
        cfg = dlg.settings()
        self.project.export_dir = cfg.out_dir
        self.project.document_name = cfg.document
        exporter = BatchExporter(edits, cfg)
        ProgressDialog(exporter, self).exec()

    # --------------------------------------------------------------- misc
    def clear_cache(self) -> None:
        size = proxycache.cache_size()
        if QMessageBox.question(
                self, "Clear proxy cache",
                f"Delete {size / 1e6:.0f} MB of cached previews?\n"
                "They will be rebuilt the next time you open each page.") \
                != QMessageBox.Yes:
            return
        proxycache.clear()
        # the prefetcher remembers what it has built; without this it goes on
        # believing the files it just lost are still there and refuses to make
        # them again for the rest of the session
        self.prefetch.reset()
        self.queue.forget_thumbs()
        self.statusBar().showMessage("Proxy cache cleared", 3000)

    def show_help(self) -> None:
        QMessageBox.information(self, "Shortcuts", HELP_TEXT)

    def _restore_geometry(self) -> None:
        g = self.settings.value("geometry")
        if g:
            self.restoreGeometry(g)
        s = self.settings.value("splitter")
        if s:
            self.splitter.restoreState(s)
        # once the panel has a real size to measure, put the divider on a
        # column boundary - including the width restored from last time
        QTimer.singleShot(0, self._snap_queue_width)

    def closeEvent(self, ev):
        if self._dirty and self.project.pages:
            r = QMessageBox.question(
                self, APP, "Save the project before closing?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                ev.ignore()
                return
            if r == QMessageBox.Yes:
                self.save_project()
        # Mark the teardown before releasing the import modal.  accept() only
        # schedules the nested loop to exit; the rest of this method runs
        # first, and the import would then resume into _end_import and rebuild
        # the queue on a window whose widgets are already going away.
        self._closing = True
        if self._load_dialog is not None:
            self._load_dialog.finish()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.loader.shutdown()
        self.bg.shutdown()
        self.prefetch.shutdown()
        super().closeEvent(ev)


HELP_TEXT = """\
TOOLS
  V                   cursor - pan, zoom, adjust an existing crop or blur
  C                   crop - draw ONE new crop, then it releases
  L                   alignment guide - lay down a straightedge that stays put
  B                   blur - drag out a blur over something on the page
  X                   cut - split this scan into separate pages
  Del                 delete the selected blur
  Esc                 back to the cursor

NAVIGATION
  Ctrl+←  Ctrl+→      previous / next page
  Enter               mark done and go to next
  wheel               zoom at the cursor
  middle-drag, Space  pan
  F                   fit page          1 / 2   100% / 50%

STRAIGHTENING
  D                   auto-deskew from the text lines
  [  ]                rotate by 0.01°     Shift+[ ]   by 0.10°
  Ctrl+L              rotate the page so the guide becomes level
  Shift+L             clear the guide

The guide is a straightedge, not a command: it stays where you put it while you
turn the page onto it, and it survives moving between pages so a whole document
can be squared against the same reference.  Shift constrains it while drawing,
Alt keeps a free angle instead of snapping to level.

CUTTING AND JOINING
  X                   place a cut line - Shift cuts across instead of down
  Page > Cut          measured cuts: halves, thirds, quarters
  Ctrl+Shift+J        join two scans into a single page
Cut halves and joined pages are locked, so the project-wide crop leaves them
alone.  Nothing is written until export - the source files are never modified.

CROPPING
  arrows              nudge 1 px   (Shift = 10 px, Alt = resize)
  E                   fit crop to the detected page edges
  A                   auto straighten and crop
  Ctrl+D              clear crop

By default every page in the project shares one crop, so all your exports come
out the same size.  The link button on the tool bar turns that off.

QUEUE
  Delete              remove selected pages (files on disk are untouched)
  drag                reorder
  Ctrl+Shift+↑ ↓      move selected pages up / down
  Ctrl+M              move selected pages to a chosen position
  right-click         document name, spread, move to top/bottom, reveal

BATCH
  Ctrl+B              copy framing from previous page
  Ctrl+Shift+B        apply this framing to selected pages
  Ctrl+G              set the document name (one name for the whole set)
  Ctrl+J              align two pages as a spread
  Ctrl+Shift+M        match crops that are within a few pixels of each other
  Ctrl+E              export

HISTORY
  Ctrl+Z  Ctrl+Y      undo / redo (everything, including batch operations)
"""
