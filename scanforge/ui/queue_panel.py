"""The page queue: a filmstrip of every scan in the batch with its edit state.

Raw scans rarely come off the scanner in reading order, so this list is
editable: drag to reorder, Delete to drop pages, and a sort menu for the common
cases.  Thumbnails are cached as QPixmaps in memory - rebuilding them from disk
on every repaint is what used to make marking a page done feel sticky.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import (QObject, QPoint, QRect, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QFontMetrics, QIcon, QPainter,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                               QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QPushButton, QStyle, QStyledItemDelegate,
                               QToolButton, QVBoxLayout, QWidget)

from .. import proxycache
from ..imageio_util import RASTER_EXTS
from ..model import PageEdit, Project
from . import icons

_ICON_ROLE = Qt.UserRole + 3      # this row's picture is current
_GAP_ROLE = Qt.UserRole + 4       # this row is the slot being dragged to
_MARK_ROLE = Qt.UserRole + 5      # what has been done to this page
_NUM_ROLE = Qt.UserRole + 6       # its position in the queue
_STATE_ROLE2 = Qt.UserRole + 7    # (edited, done, selected-ish) flags

THUMB_W, THUMB_H = 104, 132

FILTERS = ["All pages", "Not done", "Done", "Edited", "Untouched", "Spreads"]


def gather_paths(paths: list) -> list:
    """Expand folders into the image files inside them, in natural order."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in RASTER_EXTS:
                        out.append(os.path.join(root, f))
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in RASTER_EXTS:
            out.append(p)
    return out


class MoveToDialog(QDialog):
    """Ask for a position, letting the user type anything.

    A spin box would silently clamp what you type, so 250 quietly becomes 40
    and the page goes somewhere you did not ask for.  Here the number stays as
    typed and the dialog tells you when it is out of range, refusing to confirm
    until it is not.
    """

    def __init__(self, count: int, current: int, what: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Move to position")
        self.count = count
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"Move {what} to position:"))

        self.edit = QLineEdit(str(current))
        self.edit.selectAll()
        self.edit.textChanged.connect(self._validate)
        v.addWidget(self.edit)

        self.note = QLabel(f"currently {current} of {count}")
        self.note.setStyleSheet("color:#9aa0aa;")
        v.addWidget(self.note)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        v.addWidget(self.buttons)
        self._validate(self.edit.text())

    def _validate(self, text: str) -> None:
        text = text.strip()
        ok, msg = True, ""
        if not text:
            ok, msg = False, "Type a position"
        else:
            try:
                n = int(text)
            except ValueError:
                ok, msg = False, "That is not a whole number"
            else:
                if n < 1:
                    ok, msg = False, f"Too low — the first position is 1"
                elif n > self.count:
                    ok, msg = False, f"Too high — there are only {self.count} pages"
        self.note.setText(msg if not ok else f"of {self.count} pages")
        self.note.setStyleSheet("color:#ff7b72;" if not ok else "color:#9aa0aa;")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(ok)

    def value(self) -> int:
        return int(self.edit.text().strip())


class ThumbBridge(QObject):
    """Marshals 'proxy is ready' from the prefetch threads onto the GUI thread."""
    ready = Signal(str)


class _PageDelegate(QStyledItemDelegate):
    """Draws one page in the queue.

    The scan itself carries its position, in a dim block in the bottom-right
    corner, the way a page number sits on a printed page.  There is no file
    name: every scan in a project comes from one folder and is named after it,
    so repeating that under all four hundred of them says nothing.  The line of
    text underneath is reserved for what has actually been *done* to the page -
    cropped, aligned, locked, finished - which is the only thing that differs
    from one to the next and the only thing worth reading at a glance.
    """

    def sizeHint(self, option, index) -> QSize:
        return QSize(THUMB_W + 14, THUMB_H + 20)

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        r = option.rect

        gap = index.data(_GAP_ROLE)
        if gap:
            self._paint_gap(p, r, int(gap))
            p.restore()
            return

        # A page the current mode has taken out of play - every page but the
        # two in an open spread - is drawn faint rather than hidden, so the
        # document still reads as one run of pages and you can see where in it
        # you are working.
        if not (option.state & QStyle.State_Enabled):
            p.setOpacity(0.32)

        selected = bool(option.state & QStyle.State_Selected)
        if selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#2f5d8a"))
            p.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 5, 5)

        tw, th = THUMB_W, THUMB_H
        thumb = QRect(r.x() + (r.width() - tw) // 2, r.y() + 4, tw, th)
        pm = index.data(Qt.DecorationRole)
        if isinstance(pm, QIcon):
            pm = pm.pixmap(tw, th)
        if isinstance(pm, QPixmap) and not pm.isNull():
            p.drawPixmap(thumb.x() + (tw - pm.width()) // 2,
                         thumb.y() + (th - pm.height()) // 2, pm)

        edited, done = (index.data(_STATE_ROLE2) or (False, False))[:2]
        p.setBrush(Qt.NoBrush)
        if done:
            p.setPen(QPen(QColor("#5fc26e"), 3))
            p.drawRect(thumb.adjusted(1, 1, -1, -1))
        elif edited:
            p.setPen(QPen(QColor("#ffb020"), 2))
            p.drawRect(thumb.adjusted(1, 1, -1, -1))

        self._paint_number(p, thumb, index.data(_NUM_ROLE))

        marks = index.data(_MARK_ROLE) or ""
        if marks:
            if selected:
                colour = "#ffffff"
            elif done:
                colour = "#7fd18a"
            elif edited:
                colour = "#d8b06a"
            else:
                colour = "#aeb4bd"
            p.setPen(QColor(colour))
            f = p.font()
            f.setPointSizeF(max(7.5, f.pointSizeF() - 0.5))
            p.setFont(f)
            p.drawText(QRect(r.x(), thumb.bottom() + 2, r.width(), 16),
                       Qt.AlignHCenter | Qt.AlignVCenter,
                       QFontMetrics(f).elidedText(marks, Qt.ElideRight,
                                                  r.width() - 4))
        p.restore()

    @staticmethod
    def _paint_number(p: QPainter, thumb: QRect, number) -> None:
        if not number:
            return
        text = str(number)
        f = p.font()
        f.setPointSizeF(max(7.5, f.pointSizeF() - 0.5))
        f.setBold(True)
        p.setFont(f)
        w = max(18, QFontMetrics(f).horizontalAdvance(text) + 10)
        box = QRect(thumb.right() - w - 3, thumb.bottom() - 18, w, 15)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 125))
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QColor(255, 255, 255, 205))
        p.drawText(box, Qt.AlignCenter, text)

    @staticmethod
    def _paint_gap(p: QPainter, r: QRect, _count: int) -> None:
        """The blank slot the dragged pages will drop into."""
        box = QRectF(r).adjusted(4, 4, -4, -4)
        # No label: the pages themselves are under the cursor, faded, and the
        # slot only has to show where they will land.
        p.setPen(QPen(QColor("#ffc247"), 2, Qt.DashLine))
        p.setBrush(QColor(255, 194, 71, 28))
        p.drawRoundedRect(box, 6, 6)


class PageList(QListWidget):
    """Icon list that reorders by opening a gap where the pages will land.

    This does not use Qt's drag and drop at all for reordering, and that is
    deliberate.  Handing the job to the platform's drag manager means the drop
    is resolved somewhere we cannot see or test - it worked in a synthetic test
    and kept failing in the actual application - and the best feedback it can
    give is a thin caret between two icons, which is a guess about where the
    page will end up rather than a picture of it.

    So a reorder is an ordinary mouse gesture on this widget.  The moment you
    drag far enough, the pages you picked up are lifted out of the list and a
    blank slot opens in their place.  The slot follows the cursor, the other
    pages shuffle around it, and where the slot is when you let go is exactly
    where the pages go.  Nothing about that is a guess.

    Dropping *files* from the file manager is still real drag and drop - that
    part works, and is the only thing the platform is asked to do.
    """

    # uids, and the index to insert them at among the pages that are staying
    reorderRequested = Signal(list, int)
    filesDropped = Signal(list)
    dragCancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos: Optional[QPoint] = None
        self._press_row = -1
        self._drag_uids: list = []
        self._gap: Optional[QListWidgetItem] = None
        self._collapse_to: Optional[QListWidgetItem] = None
        self._ghost: Optional[QPixmap] = None
        self._ghost_at: Optional[QPoint] = None
        self._grab: QPoint = QPoint(0, 0)
        self._edge = QTimer(self)
        self._edge.setInterval(40)
        self._edge.timeout.connect(self._edge_scroll)
        self._cursor_y = 0

    # ------------------------------------------------------------- the gesture
    @property
    def dragging(self) -> bool:
        return self._gap is not None

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            super().mousePressEvent(ev)
            return
        pos = ev.position().toPoint()
        self._press_pos = pos
        self._press_row = self.indexAt(pos).row()
        item = self.item(self._press_row) if self._press_row >= 0 else None
        plain = not (ev.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier))

        if item is not None and item.isSelected() and plain:
            # Hold the selection until the button comes up.  Pressing on one of
            # several selected pages makes Qt collapse the selection to that one
            # page immediately - it only defers that to release when its own
            # drag machinery is switched on, and ours replaced it.  That is why
            # dragging more than one page stopped working: by the time the drag
            # began there was only ever one page selected.
            self._collapse_to = item
            return
        self._collapse_to = None
        if self._press_row < 0:
            # Empty space.  Deselect and stop there rather than letting the view
            # start a rubber band: a sweep selection over a grid of scans is not
            # something this list wants, and it fought the reorder gesture for
            # the same button.  Ctrl and Shift click are how several pages get
            # picked, and they still work exactly as they did.
            if not (ev.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
                self.clearSelection()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        pos = ev.position().toPoint()
        self._cursor_y = pos.y()
        if self.dragging:
            self._move_ghost(pos)
            self._move_gap(pos)
            self._edge.start()
            return
        if (self._press_pos is not None and self._press_row >= 0
                and (ev.buttons() & Qt.LeftButton)
                and (pos - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            self._lift()
            self._move_ghost(pos)
            self._move_gap(pos)
            return
        if self._press_pos is not None and (ev.buttons() & Qt.LeftButton):
            return          # a held button belongs to the drag, not to a sweep
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self.dragging:
            self._drop()
            return
        if self._collapse_to is not None:
            # a click, not a drag: now do what the press would have done
            item, self._collapse_to = self._collapse_to, None
            self.clearSelection()
            item.setSelected(True)
            self.setCurrentItem(item)
        self._press_pos = None
        self._press_row = -1
        super().mouseReleaseEvent(ev)

    def keyPressEvent(self, ev):
        if self.dragging and ev.key() == Qt.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(ev)

    def focusOutEvent(self, ev):
        # never leave the list with pages lifted out of it
        if self.dragging:
            self._cancel()
        super().focusOutEvent(ev)

    # ------------------------------------------------------------- lift / drop
    def _lift(self) -> None:
        """Take the picked pages out and open a slot where they were."""
        picked = {i.data(Qt.UserRole) for i in self.selectedItems()}
        under = self.item(self._press_row)
        if under is None:
            return
        if under.data(Qt.UserRole) not in picked:
            # dragging something that was not selected: take just that one
            self.clearSelection()
            under.setSelected(True)
            self.setCurrentItem(under)
            picked = {under.data(Qt.UserRole)}
        self._collapse_to = None          # a drag is not a click

        rows = [i for i in range(self.count())
                if self.item(i).data(Qt.UserRole) in picked]
        if not rows:
            return
        self._drag_uids = [self.item(i).data(Qt.UserRole) for i in rows]
        # where in the tile the page was picked up, so it does not jump under
        # the cursor the moment it lifts
        r = self.visualItemRect(under)
        self._grab = (self._press_pos - r.topLeft()) if self._press_pos \
            else QPoint(r.width() // 2, r.height() // 2)
        self._ghost = self._make_ghost(rows)

        at = rows[0]
        for i in reversed(rows):
            self.takeItem(i)

        gap = QListWidgetItem()
        gap.setData(_GAP_ROLE, len(self._drag_uids))
        gap.setFlags(Qt.NoItemFlags)
        self.insertItem(min(at, self.count()), gap)
        self._gap = gap
        self.setCursor(Qt.ClosedHandCursor)

    def _make_ghost(self, rows: list) -> QPixmap:
        """The page itself, faded, to carry around under the cursor.

        More than one page draws as a small stack with the count on it, so a
        multi-page drag looks like what it is rather than like a single page.
        """
        first = self.item(rows[0])
        base = first.data(Qt.DecorationRole)
        if isinstance(base, QIcon):
            base = base.pixmap(THUMB_W, THUMB_H)
        if not isinstance(base, QPixmap) or base.isNull():
            base = QPixmap(THUMB_W, THUMB_H)
            base.fill(QColor("#3a3e46"))

        extra = min(2, len(rows) - 1)
        step = 5
        pm = QPixmap(base.width() + extra * step, base.height() + extra * step)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        for i in range(extra, -1, -1):
            x, y = i * step, i * step
            p.setOpacity(0.55 if i else 1.0)
            p.drawPixmap(x, y, base)
            p.setOpacity(1.0)
            p.setPen(QPen(QColor("#ffc247"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(x + 1, y + 1, base.width() - 2, base.height() - 2)
        if len(rows) > 1:
            text = str(len(rows))
            f = p.font()
            f.setBold(True)
            f.setPointSizeF(f.pointSizeF() + 1.5)
            p.setFont(f)
            w = max(22, QFontMetrics(f).horizontalAdvance(text) + 12)
            box = QRect(4, 4, w, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ffc247"))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor("#241c07"))
            p.drawText(box, Qt.AlignCenter, text)
        p.end()
        return pm

    def _ghost_rect(self, at: Optional[QPoint] = None) -> QRect:
        at = at or self._ghost_at
        if self._ghost is None or at is None:
            return QRect()
        return QRect(at - self._grab, self._ghost.size())

    def _move_ghost(self, pos: QPoint) -> None:
        old = self._ghost_rect()
        self._ghost_at = pos
        new = self._ghost_rect()
        if self._ghost is not None:
            self.viewport().update(old.united(new).adjusted(-4, -4, 4, 4))

    def _move_gap(self, pos: QPoint) -> None:
        if self._gap is None:
            return
        # `_slot_at` says which visual position the cursor is over, and taking
        # the slot out and putting it back at that index lands it exactly
        # there.  An earlier version subtracted one when moving forwards, on
        # the reasoning that removing the slot shifts the items after it left -
        # true of the items, but not of the index the slot is going to, so the
        # slot sat one cell behind the cursor the whole way.
        target = max(0, min(self._slot_at(pos), self.count() - 1))
        cur = self.row(self._gap)
        if target == cur:
            return
        self.takeItem(cur)
        self.insertItem(target, self._gap)

    def _drop(self) -> None:
        self._edge.stop()
        self.unsetCursor()
        self._clear_ghost()
        row = self.row(self._gap)
        self.takeItem(row)
        self._gap = None
        uids, self._drag_uids = self._drag_uids, []
        self._press_pos = None
        self._press_row = -1
        if uids:
            # `row` counts only the pages that stayed, because the dragged ones
            # are physically out of the list - so it needs no correction
            self.reorderRequested.emit(uids, row)

    def _cancel(self) -> None:
        self._edge.stop()
        self.unsetCursor()
        self._clear_ghost()
        if self._gap is not None:
            self.takeItem(self.row(self._gap))
            self._gap = None
        self._drag_uids = []
        self._press_pos = None
        self._press_row = -1
        self.dragCancelled.emit()

    def _clear_ghost(self) -> None:
        rect = self._ghost_rect()
        self._ghost = None
        self._ghost_at = None
        self._collapse_to = None
        if not rect.isNull():
            self.viewport().update(rect.adjusted(-4, -4, 4, 4))

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self._ghost is None or self._ghost_at is None:
            return
        p = QPainter(self.viewport())
        p.setOpacity(0.62)
        p.drawPixmap(self._ghost_rect().topLeft(), self._ghost)
        p.end()

    def _edge_scroll(self) -> None:
        """Keep scrolling while the cursor is held near the top or bottom."""
        if not self.dragging:
            self._edge.stop()
            return
        h = self.viewport().height()
        bar = self.verticalScrollBar()
        if self._cursor_y < 28:
            bar.setValue(bar.value() - bar.singleStep())
        elif self._cursor_y > h - 28:
            bar.setValue(bar.value() + bar.singleStep())

    # --------------------------------------------------------- slot geometry
    def _slot_at(self, pos) -> int:
        """Which slot the cursor is over, worked out from the grid itself.

        Deliberately arithmetic rather than a hit test against the items.  The
        items move as the slot moves, so asking "what is under the cursor" gave
        an answer that depended on where the slot already was: it could shift
        one way, change the layout, and then read back the other way on the
        next mouse move.  That is the drift - the slot creeping a position out
        of step with the cursor and sometimes oscillating between two.

        The grid is uniform, so the cell under a point is a division.  The
        answer depends on nothing but the cursor, which is what "follows the
        cursor" has to mean.
        """
        n = self.count()
        if n == 0:
            return 0
        p = pos.toPoint() if hasattr(pos, "toPoint") else pos
        cw = self.gridSize().width() + 2 * self.spacing()
        chh = self.gridSize().height() + 2 * self.spacing()
        if cw <= 0 or chh <= 0:
            return n
        r0 = self.visualItemRect(self.item(0))
        x0 = r0.left() - self.spacing()
        y0 = r0.top() - self.spacing()
        cols = max(1, self.viewport().width() // cw)

        dx, dy = p.x() - x0, p.y() - y0
        row = max(0, int(dy // chh))
        col = max(0, min(cols - 1, int(dx // cw)))
        # past the middle of a cell means "after this one"
        after = 1 if (dx - col * cw) > cw / 2 else 0
        return max(0, min(n, row * cols + col + after))

    # ------------------------------------------------------- files from disk
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            return
        ev.ignore()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            return
        ev.ignore()

    def dropEvent(self, ev):
        if not ev.mimeData().hasUrls():
            ev.ignore()
            return
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        ev.acceptProposedAction()
        if paths:
            self.filesDropped.emit(paths)


class QueuePanel(QWidget):
    pageActivated = Signal(str)          # uid
    selectionChanged = Signal(list)      # [uid]
    pagesDropped = Signal(list)          # [path]
    removeRequested = Signal(list)       # [uid]
    orderChanged = Signal(str)           # undo label
    setDocumentRequested = Signal()
    doneToggleRequested = Signal()
    spreadRequested = Signal()
    joinRequested = Signal()
    unjoinRequested = Signal()

    def __init__(self, project: Project, prefetcher: proxycache.Prefetcher, parent=None):
        super().__init__(parent)
        self.project = project
        self.prefetcher = prefetcher
        self._filter = FILTERS[0]
        self._search = ""
        self._base_pix: dict = {}        # path -> raw thumbnail pixmap
        self._icons: dict = {}           # (path, state) -> QIcon
        self._index: dict = {}           # uid -> position, so labelling is O(1)
        self._items: dict = {}           # uid -> QListWidgetItem
        # uids the current mode allows; None means the whole document
        self._restricted_to = None
        self._page_by_uid: dict = {}     # uid -> PageEdit, for the icon filler

        self.bridge = ThumbBridge()
        self.bridge.ready.connect(self._thumb_ready)
        prefetcher.on_ready = lambda p: self.bridge.ready.emit(p)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(5)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.cmb_filter = QComboBox()
        self.cmb_filter.addItems(FILTERS)
        self.cmb_filter.currentTextChanged.connect(self._set_filter)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("Filter by name…")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self._set_search)
        bar.addWidget(self.cmb_filter, 1)
        bar.addWidget(self.ed_search, 2)
        root.addLayout(bar)

        self.list = PageList()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setIconSize(QSize(THUMB_W, THUMB_H))
        self.list.setGridSize(QSize(THUMB_W + 14, THUMB_H + 20))
        self.list.setResizeMode(QListWidget.Adjust)
        # Static: items keep their grid slots and the model decides the order.
        # Snap or Free would let the view park icons at arbitrary positions
        # without ever reordering the pages behind them.
        self.list.setMovement(QListWidget.Static)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Ctrl-click and Shift-click only; no sweep selection.
        self.list.setSelectionRectVisible(False)
        self.list.setUniformItemSizes(True)
        # always reserved, so the grid never reflows when it appears
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.delegate = _PageDelegate(self.list)
        self.list.setItemDelegate(self.delegate)
        # Lay the list out in batches.  Adding four hundred items at once makes
        # Qt position every one of them before it will paint anything, which is
        # a visible stall on a big folder; batched, the first screenful appears
        # immediately and the rest fills in.
        self.list.setLayoutMode(QListWidget.Batched)
        self.list.setBatchSize(60)
        self.list.setSpacing(2)
        self.list.setAcceptDrops(True)
        # Reordering is this widget's own mouse gesture, not a platform drag;
        # Qt must not start one of its own on top of it.  Files dropped from the
        # file manager still arrive as ordinary drops.
        self.list.setDragEnabled(False)
        self.list.setDragDropMode(QAbstractItemView.DropOnly)
        self.list.setDropIndicatorShown(False)
        self.list.setAutoScroll(True)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.currentItemChanged.connect(self._current_changed)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.reorderRequested.connect(self._apply_reorder)
        self.list.filesDropped.connect(self.pagesDropped.emit)
        self.list.dragCancelled.connect(lambda: self.rebuild(self.current_uid()))
        # Thumbnails are filled in between events, for the rows on screen only.
        # A zero interval means "whenever the event loop is idle", and the timer
        # stops itself as soon as everything visible has a picture.
        self._icon_timer = QTimer(self)
        self._icon_timer.setInterval(0)
        self._icon_timer.timeout.connect(self._fill_icons)
        self.list.verticalScrollBar().valueChanged.connect(
            lambda _v: self._icon_timer.start())
        # the delegate paints the rows, including selection
        self.list.setStyleSheet(
            "QListWidget{background:#212328;border:1px solid #34373d;}")
        root.addWidget(self.list, 1)

        tools = QHBoxLayout()
        tools.setSpacing(3)
        self.btn_up = self._tool("up", "Move up  (Ctrl+Shift+Up)",
                                 lambda: self._move(-1))
        self.btn_down = self._tool("down", "Move down  (Ctrl+Shift+Down)",
                                   lambda: self._move(1))
        self.btn_goto = self._tool("goto", "Move to position…  (Ctrl+M)",
                                   self.move_to_position)
        self.btn_sort = self._tool("sort", "Sort the queue", self._sort_menu)
        self.btn_del = self._tool("trash", "Remove from queue  (Delete)",
                                  lambda: self.removeRequested.emit(self.selected_uids()))
        for b in (self.btn_up, self.btn_down, self.btn_goto, self.btn_sort):
            tools.addWidget(b)
        tools.addStretch(1)
        tools.addWidget(self.btn_del)
        root.addLayout(tools)

        self.lbl_count = QLabel("0 pages")
        self.lbl_count.setStyleSheet("color:#9aa0aa;")
        self.lbl_count.setWordWrap(True)
        root.addWidget(self.lbl_count)

    def _tool(self, name: str, tip: str, slot) -> QToolButton:
        b = QToolButton()
        b.setIcon(icons.icon(name, 18))
        b.setIconSize(QSize(18, 18))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setFixedSize(26, 24)
        b.clicked.connect(slot)
        return b

    # ------------------------------------------------------------ reordering
    def _move(self, delta: int) -> None:
        uids = self.selected_uids()
        if not uids:
            return
        if self.project.move(uids, delta):
            self.orderChanged.emit("Move pages")
            self.rebuild()
            for u in uids:
                self.select(u, add=True)

    def _apply_reorder(self, uids: list, row: int) -> None:
        """Land the dragged pages at `row` among the pages that stayed put.

        No correction is needed for pages that sat before the drop: while you
        were dragging they were physically out of the list, so the slot's index
        already counts only the pages that remain.  The old caret reported a row
        in the *undisturbed* list and had to be adjusted for that, which is the
        kind of arithmetic that quietly lands a page one place off.
        """
        picked = set(uids)
        # The pages being moved come from the caller, not from the list: at the
        # moment of the drop they are lifted *out* of it, so reading them back
        # off the widget would find nothing and silently do nothing at all.
        moving = sorted(picked, key=lambda u: self.project.index_of(u))
        moving = [u for u in moving if self.project.by_uid(u) is not None]
        if not moving:
            return
        rest = [u for u in self.visible_uids() if u not in picked]
        at = max(0, min(len(rest), row))
        new_order = rest[:at] + moving + rest[at:]
        # what the order is now, for the same set of pages - taken from the
        # project rather than the widget, which is mid-drag and missing some
        involved = set(new_order)
        current = [p.uid for p in self.project.pages if p.uid in involved]
        if new_order == current:
            return
        self.orderChanged.emit("Reorder pages")
        self.project.reorder_visible(new_order)
        self._reselect(moving)

    def _reselect(self, uids: list) -> None:
        self.rebuild()
        if uids:
            self.select(uids[0])
            for u in uids[1:]:
                self.select(u, add=True)

    def move_to_position(self) -> None:
        """Move the selected pages to an explicit position in the queue."""
        uids = self.selected_uids()
        n = len(self.project.pages)
        if not uids or not n:
            return
        ordered = sorted(uids, key=self.project.index_of)
        current = self.project.index_of(ordered[0]) + 1
        what = (f"“{self.project.by_uid(ordered[0]).name}”" if len(ordered) == 1
                else f"{len(ordered)} pages")
        dlg = MoveToDialog(n, current, what, self)
        if dlg.exec() != QDialog.Accepted:
            return
        pos = dlg.value()
        if pos == current:
            return
        self.orderChanged.emit("Move to position")
        self.project.move_to(ordered, pos)
        self._reselect(ordered)

    def _sort_menu(self) -> None:
        m = QMenu(self)
        for label, key, rev in (("By file name", "name", False),
                                ("By file name, reversed", "name", True),
                                ("By date modified", "modified", False),
                                ("By file size", "size", False),
                                ("Reverse current order", "reverse", False)):
            act = QAction(label, m)
            act.triggered.connect(
                lambda _=False, k=key, r=rev: self._do_sort(k, r))
            m.addAction(act)
        m.exec(self.btn_sort.mapToGlobal(self.btn_sort.rect().bottomLeft()))

    def _do_sort(self, key: str, reverse: bool) -> None:
        self.orderChanged.emit("Sort pages")
        self.project.sort_pages(key, reverse)
        self.rebuild(self.current_uid())

    def _context_menu(self, pos) -> None:
        uids = self.selected_uids()
        m = QMenu(self)
        a_doc = m.addAction(icons.icon("save_as", 16), "Set document name…")
        a_doc.triggered.connect(self.setDocumentRequested.emit)
        a_spread = m.addAction(icons.icon("spread", 16), "Align as spread…")
        a_spread.triggered.connect(self.spreadRequested.emit)
        a_join = m.addAction(icons.icon("spread", 16), "Join into one page…")
        a_join.triggered.connect(self.joinRequested.emit)
        a_join.setEnabled(len(uids) >= 2)
        pages = [self.project.by_uid(u) for u in uids]
        a_unjoin = m.addAction("Split joined page apart")
        a_unjoin.triggered.connect(self.unjoinRequested.emit)
        a_unjoin.setEnabled(any(p is not None and p.join for p in pages))
        m.addSeparator()
        # Marking a page done had no inverse anywhere in the queue: Enter set
        # it, and taking it back meant finding the page again and unticking a
        # box in the inspector, one page at a time.
        live = [p for p in pages if p is not None]
        all_done = bool(live) and all(p.done for p in live)
        a_done = m.addAction("Mark not done" if all_done else "Mark done")
        a_done.setShortcut("Ctrl+Return")
        a_done.triggered.connect(self.doneToggleRequested.emit)
        a_done.setEnabled(bool(live))
        m.addSeparator()
        a_up = m.addAction(icons.icon("up", 16), "Move up")
        a_up.triggered.connect(lambda: self._move(-1))
        a_dn = m.addAction(icons.icon("down", 16), "Move down")
        a_dn.triggered.connect(lambda: self._move(1))
        a_goto = m.addAction(icons.icon("goto", 16), "Move to position…")
        a_goto.triggered.connect(self.move_to_position)
        a_top = m.addAction("Move to top")
        a_top.triggered.connect(lambda: self._move_end(True))
        a_bot = m.addAction("Move to bottom")
        a_bot.triggered.connect(lambda: self._move_end(False))
        m.addSeparator()
        a_show = m.addAction("Show in Explorer")
        a_show.triggered.connect(self._reveal)
        m.addSeparator()
        a_del = m.addAction(icons.icon("trash", 16),
                            f"Remove {len(uids)} page(s) from queue")
        a_del.triggered.connect(lambda: self.removeRequested.emit(uids))
        for a in (a_doc, a_spread, a_up, a_dn, a_goto, a_top, a_bot, a_del,
                  a_show):
            a.setEnabled(bool(uids))
        if not uids:
            a_join.setEnabled(False)
            a_unjoin.setEnabled(False)
        m.exec(self.list.mapToGlobal(pos))

    def _move_end(self, to_top: bool) -> None:
        uids = set(self.selected_uids())
        if not uids:
            return
        self.orderChanged.emit("Move pages")
        picked = [p for p in self.project.pages if p.uid in uids]
        rest = [p for p in self.project.pages if p.uid not in uids]
        self.project.pages = picked + rest if to_top else rest + picked
        self.rebuild()
        for u in uids:
            self.select(u, add=True)

    def _reveal(self) -> None:
        uid = self.current_uid()
        page = self.project.by_uid(uid) if uid else None
        if not page or not os.path.exists(page.path):
            return
        if os.name == "nt":
            os.system(f'explorer /select,"{os.path.normpath(page.path)}"')
        else:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(page.path)))

    # ----------------------------------------------------------------- rebuild
    def rebuild(self, keep_uid: Optional[str] = None) -> None:
        """Put the rows up immediately; fill the pictures in afterwards.

        Reading a thumbnail is a JPEG decode, a scale and a paint - about three
        milliseconds.  Doing that for every page while building the list meant
        the interface thread disappeared for a second on a 300-page folder and
        several on a big one, every time the queue was rebuilt: at the end of an
        import, and again on every sort, filter and undo.  That is what "the
        program stops responding while it caches" actually was - not the
        decoding, which is on worker threads, but the thumbnailing after it.

        So rows appear at once with a placeholder, and `_fill_icons` fills in
        the ones you can actually see, a few per pass, between events.
        """
        self.list.blockSignals(True)
        self.list.clear()
        # position lookup, so labelling the list is linear rather than quadratic
        self._index = {p.uid: i for i, p in enumerate(self.project.pages)}
        self._page_by_uid = {p.uid: p for p in self.project.pages}
        self._items = {}
        placeholder = self._placeholder()
        for page in self.project.pages:
            if not self._passes(page):
                continue
            item = QListWidgetItem()
            self._items[page.uid] = item
            item.setData(Qt.UserRole, page.uid)
            item.setData(Qt.DecorationRole, placeholder)
            self._set_row_data(item, page)
            item.setToolTip(f"{page.name}\n{page.src_w} × {page.src_h} px, "
                            f"{page.dpi:g} dpi\n{page.path}")
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.update_counts()
        if keep_uid:
            self.select(keep_uid)
        self._icon_timer.start()

    # -------------------------------------------------------- lazy thumbnails
    def _visible_rows(self) -> tuple:
        """The row range on screen, with a margin either side so a small scroll
        lands on pictures that are already there."""
        lst = self.list
        n = lst.count()
        if not n:
            return (0, -1)
        vp = lst.viewport().rect()
        first = lst.indexAt(vp.topLeft())
        last = lst.indexAt(vp.bottomRight())
        lo = first.row() if first.isValid() else 0
        hi = last.row() if last.isValid() else -1
        if hi < lo:
            # the layout is still being built (it happens in batches), so take
            # a screenful from the top rather than guessing
            hi = min(n - 1, lo + 60)
        return (max(0, lo - 8), min(n - 1, hi + 24))

    def _fill_icons(self) -> None:
        lst = self.list
        lo, hi = self._visible_rows()
        filled = 0
        for i in range(lo, hi + 1):
            item = lst.item(i)
            if item is None or item.data(_ICON_ROLE):
                continue
            page = self._page_by_uid.get(item.data(Qt.UserRole))
            if page is None:
                continue
            item.setData(Qt.DecorationRole, self._thumb(page))
            item.setData(_ICON_ROLE, True)
            filled += 1
            if filled >= 8:
                return                 # come back on the next pass
        if not filled:
            self._icon_timer.stop()

    def _invalidate_icons(self) -> None:
        for item in self._items.values():
            item.setData(_ICON_ROLE, None)
        self._icon_timer.start()

    def _set_row_data(self, item: QListWidgetItem, page: PageEdit) -> None:
        """Everything the delegate needs, refreshed without touching the picture."""
        item.setData(_NUM_ROLE, self._index.get(page.uid, 0) + 1)
        item.setData(_MARK_ROLE, self._label(page))
        item.setData(_STATE_ROLE2, (page.is_edited(), page.done))
        self._apply_restriction(item, page)

    def _apply_restriction(self, item: QListWidgetItem, page: PageEdit) -> None:
        allowed = self._restricted_to
        if allowed is None:
            item.setFlags(item.flags() | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            return
        on = page.uid in allowed
        f = item.flags()
        if on:
            item.setFlags(f | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        else:
            item.setFlags(f & ~Qt.ItemIsEnabled & ~Qt.ItemIsSelectable)

    def restrict_to(self, uids) -> None:
        """Take every page but these out of play.

        The aligner works on two pages; leaving the rest of the queue live let
        a third page be clicked into the inspector while the aligner went on
        editing the original pair, so the panel that was open no longer
        described the pages on screen.
        """
        self._restricted_to = set(uids) if uids is not None else None
        for uid, item in self._items.items():
            page = self._page_by_uid.get(uid)
            if page is not None:
                self._apply_restriction(item, page)
        self.list.viewport().update()

    def clear_restriction(self) -> None:
        self.restrict_to(None)

    def update_counts(self) -> None:
        total = len(self.project.pages)
        done = sum(1 for p in self.project.pages if p.done)
        edited = sum(1 for p in self.project.pages if p.is_edited())
        size = self.project.uniform_size()
        locked = len(self.project.locked_pages())
        note = (f" · all {size[0]}×{size[1]}" if size
                else " · sizes differ" if total > 1 else "")
        if locked:
            note += f" · {locked} locked 🔒"
        self.lbl_count.setText(
            f"{total} pages · {edited} edited · {done} done{note}")

    def _passes(self, page: PageEdit) -> bool:
        if self._search and self._search not in page.name.lower() \
                and self._search not in (page.document or "").lower():
            return False
        f = self._filter
        if f == "Not done":
            return not page.done
        if f == "Done":
            return page.done
        if f == "Edited":
            return page.is_edited()
        if f == "Untouched":
            return not page.is_edited()
        if f == "Spreads":
            return bool(page.spread_partner)
        return True

    def _label(self, page: PageEdit) -> str:
        """What has been *done* to this page - nothing else.

        The position lives on the scan itself now, in the corner, and the file
        name is not worth four hundred repetitions of the folder name you
        already know.  An untouched page therefore says nothing at all, which
        makes the ones that do say something easy to find.
        """
        marks = []
        if page.label:
            # the halves of a cut scan share a file name; this is what tells
            # them apart, and it is a modification, so it belongs here
            marks.append(page.label)
        if page.join:
            marks.append("joined")
        if page.spread_partner:
            marks.append("spread")
        if page.crop_mode == "locked":
            marks.append("locked")
        elif page.is_edited():
            marks.append("edited")
        if page.done:
            marks.append("done")
        # words rather than symbols: at this size a glyph is a puzzle, and
        # there is room for one or two plain ones
        return " · ".join(marks[:2])

    # ------------------------------------------------------------- thumbnails
    @staticmethod
    def _state_key(page: PageEdit) -> tuple:
        # Only what changes the *picture*.  Edited, done and selected are drawn
        # by the delegate, so they must not be in here - keying on them threw
        # the cached thumbnail away every time a page was marked done.  The
        # label and crop stay, because the halves of a cut scan share a source
        # file and would otherwise both show the whole sheet.
        return (page.label,
                tuple(int(v) for v in page.crop) if page.crop else None)

    def _thumb(self, page: PageEdit) -> QPixmap:
        """The scan's picture alone.

        No frames, no page number, no state: the delegate draws those, so this
        stays cacheable across reordering and marking done - which used to
        throw the cached picture away and decode the JPEG again.
        """
        key = (page.path, self._state_key(page))
        cached = self._icons.get(key)
        if cached is not None:
            return cached

        base = self._base_pix.get(page.path)
        if base is None:
            f = proxycache._cache_file(page.path, "thumb")
            if os.path.exists(f):
                pm = QPixmap(f)
                if not pm.isNull():
                    base = pm.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
                    self._base_pix[page.path] = base
        if base is None:
            self.prefetcher.request(page.path, tiers=("thumb",))
            return self._placeholder()

        pm = self._centred(self._framed(base, page))
        self._icons[key] = pm
        return pm

    @staticmethod
    def _framed(base: QPixmap, page: PageEdit) -> QPixmap:
        """Show a cut piece as the piece, not as the whole sheet it came from.

        Only the pieces get this: a normal page's thumbnail is more useful
        showing everything on the glass, including whatever the crop leaves
        out.  The sub-degree straightening angle is ignored at 76 pixels.
        """
        if not page.label or not page.crop or page.join:
            return base
        fw, fh = page.base_size()
        if not fw or not fh:
            return base
        x, y, w, h = page.crop
        sx, sy = base.width() / float(fw), base.height() / float(fh)
        r = QRect(int(x * sx), int(y * sy), max(1, int(w * sx)),
                  max(1, int(h * sy))).intersected(base.rect())
        if r.width() < 2 or r.height() < 2:
            return base
        return base.copy(r).scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio,
                                   Qt.SmoothTransformation)

    def _placeholder(self) -> QPixmap:
        pm = self._base_pix.get("__placeholder__")
        if pm is not None:
            return pm
        pm = QPixmap(THUMB_W, THUMB_H)
        pm.fill(QColor("#2b2e34"))
        p = QPainter(pm)
        p.setPen(QPen(QColor("#565b64")))
        p.drawRect(0, 0, THUMB_W - 1, THUMB_H - 1)
        p.drawText(pm.rect(), Qt.AlignCenter, "loading…")
        p.end()
        self._base_pix["__placeholder__"] = pm
        return pm

    @staticmethod
    def _centred(base: QPixmap) -> QPixmap:
        """One tile size for every scan, whatever shape it is."""
        canvas = QPixmap(THUMB_W, THUMB_H)
        canvas.fill(QColor("#2b2e34"))
        p = QPainter(canvas)
        p.drawPixmap((THUMB_W - base.width()) // 2,
                     (THUMB_H - base.height()) // 2, base)
        p.end()
        return canvas

    def _thumb_ready(self, path: str) -> None:
        """A proxy landed, so this page's thumbnail can be drawn now.

        The picture is not built here - that is a JPEG decode on the interface
        thread, and during an import these arrive several a second.  The row is
        just marked stale and the filler picks it up if it is on screen.
        """
        self._base_pix.pop(path, None)
        for page in self.project.pages:
            if page.path == path:
                item = self._items.get(page.uid)
                if item is not None:
                    item.setData(_ICON_ROLE, None)
                    self._icon_timer.start()
                return

    def refresh_item(self, uid: str) -> None:
        page = self.project.by_uid(uid)
        if page is None:
            return
        item = self._items.get(uid)
        if item is not None:
            # only the row's state changed; the picture is unaffected, so it is
            # left alone rather than decoded again
            self._set_row_data(item, page)
        self.update_counts()

    def refresh_all_labels(self) -> None:
        for uid, item in self._items.items():
            page = self._page_by_uid.get(uid) or self.project.by_uid(uid)
            if page:
                self._set_row_data(item, page)
        self.update_counts()

    # -------------------------------------------------------------- selection
    def select(self, uid: str, add: bool = False) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == uid:
                if add:
                    self.list.item(i).setSelected(True)
                else:
                    self.list.setCurrentRow(i)
                self.list.scrollToItem(self.list.item(i))
                return

    def current_uid(self) -> Optional[str]:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def selected_uids(self) -> list:
        out = [i.data(Qt.UserRole) for i in self.list.selectedItems()]
        if not out and self.current_uid():
            out = [self.current_uid()]
        return out

    def visible_uids(self) -> list:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def step(self, delta: int) -> Optional[str]:
        row = self.list.currentRow()
        if row < 0:
            row = 0 if delta > 0 else self.list.count() - 1
        else:
            row += delta
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
            return self.list.item(row).data(Qt.UserRole)
        return None

    def _current_changed(self, cur, _prev):
        if cur:
            self.pageActivated.emit(cur.data(Qt.UserRole))

    def _selection_changed(self):
        self.selectionChanged.emit(self.selected_uids())

    # ------------------------------------------------------- fitting columns
    def column_width(self) -> int:
        """One grid column, including the spacing either side of it."""
        return self.list.gridSize().width() + 2 * self.list.spacing()

    def chrome_width(self) -> int:
        """Everything that is not grid: margins, frame, scroll bar, and the
        spacing the view keeps at the two ends of a row.

        Measured from the widget once it has been laid out, because the parts
        do not add up to what Qt actually reserves - guessing it from margins
        and a scroll bar's size hint was eleven pixels short, which is enough to
        lose a whole column.  The scroll bar is always shown so this stays a
        constant: one that appears when the list grows would silently take a
        column with it.
        """
        vw = self.list.viewport().width()
        gap = 2 * self.list.spacing()
        if vw > 0 and self.width() > vw:
            return self.width() - vw + gap
        m = self.contentsMargins()
        return (m.left() + m.right() + 2 * self.list.frameWidth()
                + self.list.verticalScrollBar().sizeHint().width() + 14 + gap)

    def columns_for_width(self, w: int) -> int:
        return max(1, int(round((w - self.chrome_width())
                                / float(self.column_width()))))

    def width_for_columns(self, n: int) -> int:
        return max(1, int(n)) * self.column_width() + self.chrome_width()

    def snapped_width(self, w: int) -> int:
        """The nearest width that holds a whole number of pages.

        A panel a column and a bit wide wastes the bit: the grid cannot use it
        and the pages do not grow into it.  So the splitter lands on column
        boundaries and nowhere else.
        """
        return self.width_for_columns(self.columns_for_width(w))

    def _set_filter(self, name: str):
        self._filter = name
        self.rebuild(self.current_uid())

    def _set_search(self, text: str):
        self._search = text.strip().lower()
        self.rebuild(self.current_uid())

    def forget_thumbs(self, paths=None) -> None:
        """Drop cached thumbnails so they are read again from the cache files.

        Only call this when the *pixels* behind a thumbnail have changed - a
        new proxy was written, or a page now points somewhere else.  It is not
        needed when a page's state changes, because the icon cache is keyed by
        that state and decorations are redrawn anyway; throwing the whole cache
        away instead meant every undo re-read several hundred JPEGs from disk
        on the interface thread, which is a freeze you can feel.
        """
        if paths is None:
            self._base_pix.clear()
            self._icons.clear()
        else:
            for p in paths:
                self._base_pix.pop(p, None)
            self._icons = {k: v for k, v in self._icons.items()
                           if k[0] not in set(paths)}
        self._invalidate_icons()

    def drop_stale_icons(self) -> None:
        """Forget decorated icons for pages that no longer exist, keeping the
        decoded thumbnails themselves."""
        live = {p.path for p in self.project.pages}
        self._icons = {k: v for k, v in self._icons.items() if k[0] in live}
        self._base_pix = {k: v for k, v in self._base_pix.items()
                          if k in live or k == "__placeholder__"}

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Delete and self.selected_uids():
            self.removeRequested.emit(self.selected_uids())
            return
        super().keyPressEvent(ev)
