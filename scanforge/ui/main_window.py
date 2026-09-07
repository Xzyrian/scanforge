"""ScanForge main window."""

from __future__ import annotations

import os
import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, QSettings, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence, QPalette
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QScrollArea, QSlider, QSplitter, QStackedWidget,
                               QStatusBar, QToolBar, QToolButton, QVBoxLayout,
                               QWidget)

from .. import autodetect, imageio_util as iio, proxycache, render as rnd
from ..export import BatchExporter, ExportSettings
from ..geometry import Rect
from ..history import History
from .. import model as mdl
from ..model import PageEdit, Project
from . import icons
from .angle_strip import AngleStrip
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


class ToolColumn(QWidget):
    """The editing tools, two abreast, in a column beside the canvas.

    A QToolBar can only be one button wide when it stands on end, and sixteen
    tools in a single file is taller than most windows.  Two columns with a
    rule between each group fits them all without scrolling and keeps the
    grouping legible - which a horizontal bar could only express with a
    separator you have to squint at.

    `actions()` returns them in order, separators included as their own
    entries, so the layout can be asserted rather than eyeballed.
    """

    def __init__(self, groups: list, icon_px: int, parent=None):
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(5, 7, 5, 7)
        grid.setSpacing(3)
        row = 0
        for gi, group in enumerate(groups):
            if gi:
                # The rule earns its keep only if the eye finds it: a hairline
                # the colour of the background separates nothing.  Given room
                # above and below, it reads as a break rather than as another
                # row of buttons.
                holder = QWidget()
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(2, 5, 2, 5)
                rule = QFrame()
                rule.setFrameShape(QFrame.HLine)
                rule.setFrameShadow(QFrame.Plain)
                rule.setStyleSheet("background:#4c515a;border:none;")
                rule.setFixedHeight(1)
                hl.addWidget(rule)
                grid.addWidget(holder, row, 0, 1, 2)
                sep = QAction(self)
                sep.setSeparator(True)
                self.addAction(sep)
                row += 1
            for i, act in enumerate(group):
                b = QToolButton(self)
                b.setDefaultAction(act)
                b.setIconSize(QSize(icon_px, icon_px))
                b.setAutoRaise(True)
                b.setFixedSize(icon_px + 12, icon_px + 10)
                grid.addWidget(b, row + i // 2, i % 2)
                self.addAction(act)
            row += (len(group) + 1) // 2
        grid.setRowStretch(row, 1)
        self.setFixedWidth(2 * (icon_px + 12) + 3 + 8)


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

        # Tools, then the page you are working on, then the pages you are
        # working through: left to right in the order the work flows.  The
        # tools act on the page in front of you, so they sit beside it; a bar
        # across the top of the window put the crop tool as far from the crop
        # as the layout allows, and that is a trip your eye makes on every page
        # of a two-hundred-page manual.  The column itself is built after the
        # actions exist, in _build_toolbars(), and inserted at index 0.
        #
        # The canvas carries the straightening strip beneath it: a slider, the
        # number, and a way back to zero, directly under the page they turn.
        self.angle_strip = AngleStrip()
        self.angle_strip.setStyleSheet(
            "background:#24262b;border-top:1px solid #34373d;")
        self.editing = QWidget()
        ev = QVBoxLayout(self.editing)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setSpacing(0)
        ev.addWidget(self.center, 1)
        ev.addWidget(self.angle_strip)

        # The right-hand column is the page grid.  Under it, for now, is what
        # is left of the old inspector: crop numbers, blur and the reuse
        # buttons, none of which have been given a home yet.  It is a holding
        # position, not a design - the divider between them can be dragged
        # shut, and the panel goes when its contents are placed.
        # In a scroll area, so its full height is never something the splitter
        # has to honour: left to ask for what its layout wants, the panel took
        # the column and left the grid a strip shorter than one row of pages.
        self.inspector_scroll = QScrollArea()
        self.inspector_scroll.setWidget(self.inspector)
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setFrameShape(QScrollArea.NoFrame)
        self.inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inspector_scroll.setMinimumHeight(120)

        self.right_col = QSplitter(Qt.Vertical)
        self.right_col.addWidget(self.queue)
        self.right_col.addWidget(self.inspector_scroll)
        self.right_col.setStretchFactor(0, 1)
        self.right_col.setStretchFactor(1, 0)
        self.right_col.setHandleWidth(10)
        self.right_col.setChildrenCollapsible(False)
        # the grid is what this column is for: it keeps at least two rows of
        # pages whatever the panel below it would like
        self.queue.setMinimumHeight(340)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.editing)
        split.addWidget(self.right_col)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([1000, 320])
        # The default handle is about five pixels wide, and the grid's column
        # snapping is only reachable by dragging it - a target that thin makes
        # the one gesture that resizes the grid a game of patience.
        split.setHandleWidth(10)
        split.setChildrenCollapsible(False)

        # The tool column is fixed width, so it sits outside the splitter: a
        # divider beside something that cannot be resized is a control that
        # does nothing.
        self.work = QWidget()
        wl = QHBoxLayout(self.work)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        self.work_layout = wl
        wl.addWidget(split, 1)

        split.splitterMoved.connect(self._snap_queue_width)
        self.setCentralWidget(self.work)
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

        # ---- view
        self.a_fit = self._act("&Fit page", self.canvas.fit, "F", "Fit page",
                               icon="fitview")
        # No 100% or 50%: everything you look at is a 2200 px interpretation of
        # a 10000 px scan, so "actual pixels" names a thing the editor never
        # shows you.  The slider in the status bar is the zoom control.
        self.a_zoomin = self._act("Zoom in", lambda: self.canvas.zoom_by(1.25))
        self.a_zoomout = self._act("Zoom out", lambda: self.canvas.zoom_by(0.8))
        # Ctrl+= is what the key actually sends on most layouts; Ctrl++ is what
        # it says on the keycap, so both are bound and the keycap is what the
        # menu shows.
        self.a_zoomin.setShortcuts([QKeySequence("Ctrl++"), QKeySequence("Ctrl+=")])
        self.a_zoomout.setShortcut(QKeySequence("Ctrl+-"))
        self.a_grid = self._act("Show &grid", self.toggle_grid, "G", "Show grid",
                                checkable=True, icon="grid")
        self.a_dim = self._act("&Dim outside crop", self.toggle_dim, checkable=True)
        self.a_dim.setChecked(True)
        self.a_guide_h = self._act("Add &horizontal guide",
                                   lambda: self._add_guide(True), "Space",
                                   "A level line to judge a baseline against",
                                   icon="guide_h")
        self.a_guide_v = self._act("Add &vertical guide",
                                   lambda: self._add_guide(False), "Shift+Space",
                                   "An upright line to judge a margin against",
                                   icon="guide_v")
        self.a_guide_clear = self._act("Clear guides", self._clear_guides,
                                       tip="Take every guide off the page",
                                       icon="guide_clear")

        # ---- page
        self.a_next = self._act("&Next page", lambda: self.step(1), "Ctrl+Right")
        self.a_prev = self._act("&Previous page", lambda: self.step(-1), "Ctrl+Left")
        self.a_done_next = self._act("Mark &done and go to next", self.done_and_next,
                                     "Return", "Mark done and go to next",
                                     icon="done")
        self.a_done_toggle = self._act(
            "Mark done / &not done", self.toggle_done_selection, "Ctrl+Return",
            "Flip the done mark on every selected page - the inverse of Enter")
        self.a_rot_ccw = self._act("Rotate 90° &left",
                                   lambda: self._nudge_quadrant(-1), tip="Rotate 90° left",
                                   icon="rot_ccw")
        self.a_rot_cw = self._act("Rotate 90° &right",
                                  lambda: self._nudge_quadrant(1), tip="Rotate 90° right",
                                  icon="rot_cw")
        self.a_flip_h = self._act("Flip &horizontally",
                                  lambda: self._flip(True), tip="Mirror left to right",
                                  icon="flip_h")
        self.a_flip_v = self._act("Flip &vertically",
                                  lambda: self._flip(False), tip="Mirror top to bottom",
                                  icon="flip_v")
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
        self.a_tool_crop = self._act("&Crop page",
                                     lambda: self.set_tool(Tool.CROP), "C",
                                     "Crop this page only (one use, then back "
                                     "to the cursor)",
                                     checkable=True, icon="crop")
        self.a_tool_crop_all = self._act("Crop &all pages",
                                         lambda: self.set_tool(Tool.CROP_ALL),
                                         "Shift+C",
                                         "Draw one crop and give it to every "
                                         "page in the project",
                                         checkable=True, icon="crop_all")
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
        for a in (self.a_tool_cursor, self.a_tool_crop, self.a_tool_crop_all,
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
        for a in (self.a_undo, self.a_redo, None, self.a_fitpage,
                  self.a_auto_both, None, self.a_clear_crop, self.a_reset, None,
                  self.a_copy_prev, self.a_apply_doc,
                  self.a_apply_sel, None, self.a_set_doc):
            m_edit.addSeparator() if a is None else m_edit.addAction(a)

        m_view = self.menuBar().addMenu("&View")
        for a in (self.a_book, None,
                  self.a_fit, self.a_zoomin, self.a_zoomout,
                  None, self.a_grid, self.a_dim, None,
                  self.a_guide_h, self.a_guide_v, self.a_guide_clear):
            m_view.addSeparator() if a is None else m_view.addAction(a)

        m_page = self.menuBar().addMenu("&Page")
        for a in (self.a_prev, self.a_next, self.a_done_next, self.a_done_toggle,
                  None, self.a_up,
                  self.a_down, self.a_move_to, None, self.a_spread, self.a_join,
                  self.a_unjoin, None, self.a_remove):
            m_page.addSeparator() if a is None else m_page.addAction(a)
        m_cut = m_page.addMenu("&Cut into pages")
        for a in (self.a_tool_cut, None, self.a_cut_halves, self.a_cut_thirds,
                  self.a_cut_quarters, None, self.a_cut_halves_h,
                  self.a_cut_thirds_h):
            m_cut.addSeparator() if a is None else m_cut.addAction(a)

        m_tools = self.menuBar().addMenu("&Tools")
        for a in (self.a_tool_cursor, self.a_tool_crop, self.a_tool_crop_all,
                  self.a_tool_blur, self.a_tool_cut, None,
                  self.a_blur_delete, None,
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
        # The top bar is the document and the view: what you are working on,
        # and how you are looking at it.  Nothing here changes a page.
        for a in (self.a_open_folder, self.a_save, self.a_save_as, self.a_export,
                  None, self.a_undo, self.a_redo,
                  None, self.a_guide_h, self.a_guide_v, self.a_guide_clear,
                  None, self.a_grid, self.a_book):
            main.addSeparator() if a is None else main.addAction(a)
        self.addToolBar(main)
        self.toolbar_main = main

        # ...and the column beside the canvas is the editing: tools, framing,
        # the automatic passes, and the tick that finishes a page.
        self.tool_column = ToolColumn([
            [self.a_tool_cursor, self.a_tool_crop,
             self.a_tool_crop_all, self.a_tool_blur, self.a_tool_cut],
            [self.a_rot_ccw, self.a_rot_cw, self.a_flip_h, self.a_flip_v],
            [self.a_copy_prev, self.a_match_sizes,
             self.a_spread, self.a_join],
            [self.a_auto_both, self.a_fitpage],
            [self.a_done_next],
        ], ICON_PX, self)
        self.work_layout.insertWidget(0, self.tool_column)
        self.toolbar_tools = self.tool_column

    # The slider is logarithmic: the useful range runs from a whole 140 MP page
    # in a window (about 6%) to a few hundred percent, and a linear slider
    # spends nine tenths of its travel above 100% where nothing is ever set.
    ZOOM_MIN, ZOOM_MAX, ZOOM_STEPS = 3.0, 400.0, 1000

    def _zoom_to_slider(self, pct: float) -> int:
        import math as _m
        pct = max(self.ZOOM_MIN, min(self.ZOOM_MAX, pct))
        t = ((_m.log(pct) - _m.log(self.ZOOM_MIN))
             / (_m.log(self.ZOOM_MAX) - _m.log(self.ZOOM_MIN)))
        return int(round(t * self.ZOOM_STEPS))

    def _slider_to_zoom(self, v: int) -> float:
        import math as _m
        t = max(0, min(self.ZOOM_STEPS, v)) / float(self.ZOOM_STEPS)
        return _m.exp(_m.log(self.ZOOM_MIN)
                      + t * (_m.log(self.ZOOM_MAX) - _m.log(self.ZOOM_MIN)))

    def _build_statusbar(self):
        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        self.throbber = Throbber(14)
        sb.addPermanentWidget(self.throbber)
        self.lbl_pos = QLabel("—")
        self.lbl_progress = QLabel("")
        for w in (self.lbl_progress, self.lbl_pos):
            w.setStyleSheet("color:#9aa0aa;padding:0 10px;")
            sb.addPermanentWidget(w)

        # Fit, then the zoom slider, in the far corner - the one place a zoom
        # control is always looked for, and the status bar was carrying three
        # readouts and nothing you could operate.
        self.btn_fit = QToolButton()
        self.btn_fit.setDefaultAction(self.a_fit)
        self.btn_fit.setIconSize(QSize(16, 16))
        self.btn_fit.setAutoRaise(True)
        sb.addPermanentWidget(self.btn_fit)

        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(0, self.ZOOM_STEPS)
        self.zoom_slider.setFixedWidth(150)
        self.zoom_slider.setPageStep(self.ZOOM_STEPS // 12)
        self.zoom_slider.setToolTip("Zoom — Ctrl+wheel over the page does the same")
        self.zoom_slider.valueChanged.connect(self._zoom_slider_moved)
        sb.addPermanentWidget(self.zoom_slider)

        self.lbl_zoom = QLabel("—")
        self.lbl_zoom.setMinimumWidth(52)
        self.lbl_zoom.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_zoom.setStyleSheet("color:#9aa0aa;padding:0 10px 0 4px;")
        sb.addPermanentWidget(self.lbl_zoom)
        self._zoom_guard = False
        self.setStatusBar(sb)

    def _zoom_slider_moved(self, v: int) -> None:
        if self._zoom_guard:
            return
        self.canvas.set_zoom_percent(self._slider_to_zoom(v))

    def _zoom_changed(self, pct: float) -> None:
        """The canvas moved; follow it without driving it back."""
        self._zoom_guard = True
        try:
            self.zoom_slider.setValue(self._zoom_to_slider(pct))
        finally:
            self._zoom_guard = False
        self.lbl_zoom.setText(f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%")

    def _wire(self):
        c = self.canvas
        c.editChanged.connect(self._canvas_edited)
        c.statusMessage.connect(lambda t: self.statusBar().showMessage(t, 4000))
        c.zoomChanged.connect(self._zoom_changed)
        c.cropDrawn.connect(self._crop_drawn)
        # one place to decide whether the zoom controls mean anything, fed by
        # the canvas itself rather than by every caller that loads a page
        c.pageChanged.connect(self._update_zoom_enabled)
        c.filesDropped.connect(self.take_dropped_paths)
        c.cursorMoved.connect(self._cursor_moved)
        c.interactionStarted.connect(self._begin_history)
        c.interactionEnded.connect(self._end_history)
        c.toolFinished.connect(lambda: self.set_tool(Tool.CURSOR))
        c.blurSelected.connect(self._blur_selected)
        c.cutRequested.connect(self._do_cut)
        for sb in (c.horizontalScrollBar(), c.verticalScrollBar()):
            sb.valueChanged.connect(lambda _v: self._detail_timer.start())

        self.angle_strip.angleChanged.connect(self._set_angle)

        i = self.inspector
        i.cropChanged.connect(self._set_crop)
        i.requestFitToPage.connect(self.fit_to_page)
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
        i.cropModeChanged.connect(self._set_crop_mode)

        q = self.queue
        q.pageActivated.connect(self.show_page)
        # the same route as every other drop target, so a .sfproj dropped on
        # the grid opens rather than being added as if it were a scan
        q.pagesDropped.connect(self.take_dropped_paths)
        q.removeRequested.connect(self.remove_uids)
        q.orderChanged.connect(self.history.capture)
        q.setDocumentRequested.connect(self.set_document_name)
        q.doneToggleRequested.connect(self.toggle_done_selection)
        q.spreadRequested.connect(self.open_spread)
        q.joinRequested.connect(self.join_pages)
        q.unjoinRequested.connect(self.unjoin_page)

        self.loader.ready.connect(self._on_loaded)
        self.loader.failed.connect(self._on_load_failed)
        self.bg.done.connect(self._bg_done)
        self.bg.failed.connect(self._bg_failed)

    def _settle_right_column(self) -> None:
        """Give the page grid the room and the panel what it needs.

        Splitter sizes set before a widget has been laid out are guesses; this
        runs once the column has a real height.
        """
        total = sum(self.right_col.sizes()) or self.right_col.height()
        if total <= 0:
            return
        want_panel = min(int(total * 0.42),
                         self.inspector.sizeHint().height() + 12)
        want_panel = max(120, want_panel)
        self.right_col.setSizes([max(340, total - want_panel), want_panel])

    def _snap_queue_width(self, *_args) -> None:
        """Let the queue be a whole number of pages wide, and nothing else.

        Dragging the divider to an arbitrary pixel leaves a strip the grid
        cannot use - the pages do not grow into it and nothing else can go
        there - so the divider settles on the nearest column boundary instead.

        Two columns is the floor.  One column is a list, not a grid: you lose
        the shape of the document, which is the thing the panel is for, and the
        splitter reaches that state on its own whenever the window is too
        narrow for everyone's preferred width at once.
        """
        sizes = self.splitter.sizes()
        if len(sizes) < 2 or sizes[1] <= 0:
            return
        want = max(self.queue.snapped_width(sizes[1]),
                   self.queue.width_for_columns(2))
        delta = want - sizes[1]
        self.queue.setMinimumWidth(self.queue.width_for_columns(2))
        if not delta:
            return
        # take the difference from the editing area, which is the elastic one
        sizes[1] = want
        sizes[0] = max(200, sizes[0] - delta)
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
        # Undo changes page state, never the pixels on disk, so the decoded
        # thumbnails stay valid.  Discarding them here made every Ctrl+Z re-read
        # every thumbnail from the cache on the interface thread.
        self.queue.drop_stale_icons()
        self.queue.rebuild()
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
                self.angle_strip.set_page(page)
            else:
                self.show_page(page.uid)
        else:
            self._current = None
            self.canvas.set_page(None, None)
            self.inspector.set_page(None)
            self.angle_strip.set_page(None)
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
        self.angle_strip.set_page(page)

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
        self.angle_strip.set_page(page)
        self.inspector.set_blur(-1, None)
        self._update_zoom_enabled()

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
        """A crop moved on the current page.  It stays on that page.

        This used to push the crop onto every other page whenever a
        project-wide switch was on, which meant a careful nudge on page 40 was
        copied silently over the thirty-nine before it.  Reaching the whole
        document is something you now ask for by name, with the "crop all
        pages" tool, and nothing else does it behind your back.
        """
        self._dirty = True

    def _crop_drawn(self, to_all: bool) -> None:
        """The crop tool finished a rectangle."""
        if self._current is None or not to_all:
            return
        n = self.project.apply_crop_to_all(self._current)
        if n:
            self.queue.forget_thumbs()
            self._queue_timer.start()
        locked = len(self.project.locked_pages())
        self.statusBar().showMessage(
            f"Crop given to {n + 1} pages"
            + (f", leaving {locked} locked" if locked else ""), 6000)
        self._dirty = True

    def _update_zoom_enabled(self) -> None:
        """The zoom controls only mean something with a page on the canvas.

        They used to work on an empty canvas, and the view transform stuck: the
        first page you opened then arrived at whatever magnification the
        emptiness had been left at.
        """
        on = self.canvas.edit is not None and self.canvas._pix is not None
        for w in (self.zoom_slider, self.btn_fit):
            w.setEnabled(on)
        for act in (self.a_fit, self.a_zoomin, self.a_zoomout):
            act.setEnabled(on)
        if not on:
            self.lbl_zoom.setText("—")

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
        self.angle_strip.refresh()
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

    def _nudge_quadrant(self, direction: int) -> None:
        e = self._current
        if e is None:
            return
        self._set_orientation((e.quadrant + direction) % 4, e.flip_h, e.flip_v)

    def _flip(self, horizontal: bool) -> None:
        e = self._current
        if e is None:
            return
        self._set_orientation(e.quadrant,
                              (not e.flip_h) if horizontal else e.flip_h,
                              e.flip_v if horizontal else (not e.flip_v))

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
        self.angle_strip.set_page(self._current)
        self._dirty = True

    def _set_done(self, on: bool) -> None:
        if self._current and self._current.done != on:
            # captured like every other page change: unticking the box was the
            # one edit in the app that undo could not take back
            self.history.capture("Mark done" if on else "Mark not done")
            self._current.done = on
            self.queue.refresh_item(self._current.uid)
            self._dirty = True

    def _set_crop_mode(self, mode: str) -> None:
        if self._current is None or self._current.crop_mode == mode:
            return
        self.history.capture("Crop link for this page")
        self._current.crop_mode = mode
        self._dirty = True
        self.queue.refresh_item(self._current.uid)
        self.statusBar().showMessage(
            {"linked": "This page takes a crop-all",
             "own_position": "This page keeps its own position, shared size",
             "locked": "This page's crop is locked — nothing will overwrite it",
             }.get(mode, ""), 4000)

    def set_tool(self, tool: Tool) -> None:
        self.canvas.tool = tool
        self.a_tool_cursor.setChecked(tool == Tool.CURSOR)
        self.a_tool_crop.setChecked(tool == Tool.CROP)
        self.a_tool_crop_all.setChecked(tool == Tool.CROP_ALL)
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
                "Drag to crop this page — the tool releases afterwards", 6000)
        elif tool == Tool.CROP_ALL:
            n = len(self.project.pages)
            locked = len(self.project.locked_pages())
            self.statusBar().showMessage(
                f"Drag one crop for all {n} pages"
                + (f" — {locked} locked pages keep their own" if locked else "")
                + ". The tool releases afterwards.", 8000)
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

    def _add_guide(self, horizontal: bool) -> None:
        """Drop a guide across the middle of what you are looking at.

        Guides replaced the straightedge: rather than tracing a line along a
        crooked baseline and turning the page onto it, you lay down a line that
        is level by construction and nudge the angle until the baseline meets
        it.  Same judgement, one fewer thing to get wrong - a straightedge that
        is itself a fifth of a degree out aligns the page to its own error.
        """
        if self.canvas.edit is None:
            self.statusBar().showMessage("Open a page first", 3000)
            return
        self.canvas.add_guide(horizontal)
        n = len(self.canvas.guides_h) + len(self.canvas.guides_v)
        self.statusBar().showMessage(
            f"Guide added — drag it where you need it, then nudge the angle "
            f"until the page meets it  ({n} on the page)", 6000)

    def _clear_guides(self) -> None:
        self.canvas.clear_guides()
        self.statusBar().showMessage("Guides cleared", 2500)

    def toggle_grid(self, on: bool) -> None:
        self.canvas.show_grid = on
        self.canvas.viewport().update()

    def toggle_dim(self, on: bool) -> None:
        self.canvas.dim_outside = on
        self.canvas.viewport().update()

    def clear_crop(self) -> None:
        if self._current is None:
            return
        self.history.capture("Clear crop")
        self.canvas.reset_crop()
        self._dirty = True

    def select_all_crop(self) -> None:
        self.history.capture("Whole page")
        self.canvas.select_all()

    # ----------------------------------------------------------- automations
    # These all run OpenCV over the proxy.  It only takes a fraction of a
    # second, but a fraction of a second of frozen window on every page adds up
    # to something that feels broken, so they go to a worker thread like
    # everything else and report back by signal.
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
        self.angle_strip.refresh()
        self._dirty = True
        self.statusBar().showMessage("Crop set to the detected page edges", 4000)

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
        self.angle_strip.refresh()
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
        # Hold the divider positions: adding a wider pane to the stack makes
        # the splitter take the room from the queue, which collapsed it to one
        # column and left it there after the aligner closed.
        self._sizes_before_mode = self.splitter.sizes()
        self.queue.restrict_to({left.uid, right.uid})
        self.inspector.setEnabled(False)
        self.statusBar().showMessage(
            "Aligning — the rest of the queue and the inspector are out of "
            "play until you leave; Esc leaves the aligner", 8000)

    def close_mode(self) -> None:
        """Back to the editing canvas."""
        pane = getattr(self, "_mode", None)
        if pane is None:
            return
        self._mode = None
        self.center.setCurrentWidget(self.canvas)
        self.center.removeWidget(pane)
        pane.deleteLater()
        self.queue.clear_restriction()
        self.inspector.setEnabled(True)
        sizes = getattr(self, "_sizes_before_mode", None)
        if sizes and len(sizes) == len(self.splitter.sizes()):
            self.splitter.setSizes(sizes)
        self._sizes_before_mode = None
        self._snap_queue_width()
        self.canvas.setFocus()

    def _after_spread(self, left: PageEdit, right: PageEdit) -> None:
        self._dirty = True
        self.queue.forget_thumbs([left.path, right.path])
        for p in (left, right):
            self.queue.refresh_item(p.uid)
        if self._current and self._current.uid in (left.uid, right.uid):
            self.inspector.set_page(self._current)
            self.angle_strip.set_page(self._current)
            self._reload_current()
        if left.crop_mode == "locked":
            self.statusBar().showMessage(
                "Spread saved. Both pages are locked, so \"crop all pages\" "
                "leaves their alignment alone.", 8000)

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
        wanted = set(uids)
        before = {p.uid: p.output_size() for p in self.project.pages
                  if p.uid in wanted}
        self.history.capture("Match crop sizes")
        n = self.project.match_sizes(uids, size)
        self._dirty = True
        moved = [p for p in self.project.pages
                 if p.uid in wanted and p.output_size() != before.get(p.uid)]
        # The thumbnails are keyed on the crop, so they would be rebuilt anyway
        # - but only for pages the grid happens to redraw.  Dropping them for
        # the pages that actually moved is what makes the change visible in the
        # grid rather than several scrolls later.
        self.queue.forget_thumbs([p.path for p in moved])
        self.queue.rebuild(self.queue.current_uid())
        if self._current is not None:
            self._reload_current()
        if not moved:
            self.statusBar().showMessage(
                f"All {n} pages were already {size[0]}×{size[1]} — "
                "nothing needed moving", 6000)
        else:
            biggest = max(
                max(abs(p.output_size()[0] - before[p.uid][0]),
                    abs(p.output_size()[1] - before[p.uid][1])) for p in moved)
            self.statusBar().showMessage(
                f"{len(moved)} of {n} pages resized to {size[0]}×{size[1]} — "
                f"the largest change was {biggest} px", 6000)

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
        self.queue.forget_thumbs()
        self.queue.rebuild()
        self.canvas.set_page(None, None)
        self.canvas.set_message("Drop a folder of scans here, or press Ctrl+O")
        self.inspector.set_page(None)
        self.angle_strip.set_page(None)
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
                self.angle_strip.set_page(None)
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
        if anchor is not None:
            self.project.apply_crop_to_all(anchor)
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
            self.angle_strip.set_page(None)
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
        self.take_dropped_paths(paths)
        ev.acceptProposedAction()

    def take_dropped_paths(self, paths: list) -> None:
        """Scans or a project, dropped anywhere in the window.

        One route for every drop target, so the canvas, the page grid and the
        empty chrome around them all behave the same way.  A project file wins
        over loose scans: dropping a .sfproj means "open this", not "add it".
        """
        paths = [p for p in paths if p]
        if not paths:
            return
        projects = [p for p in paths if p.lower().endswith(".sfproj")]
        if projects:
            self._open_project_file(projects[0])
        else:
            self.add_paths(paths)

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
        self.export_cfg.out_dir = proj.export_dir or self.export_cfg.out_dir
        self.history.clear()
        self.queue.forget_thumbs()
        self.queue.rebuild()

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
        # restoreState carries the handle width and the collapsible flag with
        # it, so a state saved before these were set would quietly put the
        # thin, collapsible divider back.  They are the app's choice, not the
        # session's.
        self.splitter.setHandleWidth(10)
        self.splitter.setChildrenCollapsible(False)
        # once the panel has a real size to measure, put the divider on a
        # column boundary - including the width restored from last time
        QTimer.singleShot(0, self._snap_queue_width)
        QTimer.singleShot(0, self._settle_right_column)

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
  C                   crop this page - one drag, then it releases
  Shift+C             crop every page in the project - same, but for all
  B                   blur - drag out a blur over something on the page
  X                   cut - split this scan into separate pages
  Del                 delete the selected blur
  Esc                 back to the cursor

NAVIGATION
  Ctrl+←  Ctrl+→      previous / next page
  Enter               mark done and go to next
  Ctrl+Enter          mark done / not done - the inverse of Enter
  wheel               scroll down the page
  Shift+wheel         scroll across
  Ctrl+wheel          zoom at the cursor
  Ctrl++  Ctrl+-      zoom in / out
  middle-drag          pan
  F                   fit page   ·   the slider in the status bar zooms too

STRAIGHTENING
  D                   auto-deskew from the text lines
  [  ]                rotate by 0.01°     Shift+[ ]   by 0.10°
  Space  Shift+Space  drop a level / upright guide across the view

A guide is a reference, not a command: it lands in the middle of what you are
looking at, you drag it onto a baseline or a margin, and then you turn the page
onto it.  It stays put when you change pages, so a whole document can be squared
against the same line.  These are the same guides the spread aligner uses.

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
