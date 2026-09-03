"""The modal that owns the window while a queue is being prepared.

Reading a folder of 600 dpi TIFFs takes as long as it takes, and the honest
answer to "can I use the app meanwhile" is no - letting people poke at a
half-loaded queue is how the thing wedged in the first place.  So this dialog
takes the window, shows every page and what is happening to it, and cannot be
dismissed: no close button, no Cancel, no Escape.  It goes away when the work
is done and not before.

Being modal is also what makes it *safe*.  A modal dialog runs the event loop
with input blocked, so the window keeps painting without any click reaching the
half-built project underneath - the exact thing a bare processEvents() call
could not promise.

There is exactly one exception to "no way out", and it is not a convenience.
A dialog with no close button is only safe while finishing is *guaranteed*, and
nothing that touches a disk can guarantee that: a source file on a share that
drops, a decoder that wedges, a bug that loses a notification.  So the dialog
watches its own progress, and if nothing has moved for a long time it stops
claiming to be working and offers a way out.  That is a failure report, not a
Cancel button - it cannot be reached while pages are still arriving.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QLabel, QListWidget,
                               QListWidgetItem, QProgressBar, QPushButton,
                               QStyledItemDelegate, QVBoxLayout)

QUEUED, READING, READY, FAILED = 0, 1, 2, 3


def _norm(path: str) -> str:
    """One spelling of a path, everywhere.

    The row keys and every lookup must agree exactly, or a completion lands on
    no row and a total that can never be reached keeps the window shut.
    """
    return os.path.normcase(os.path.abspath(path))

_STATE_ROLE = Qt.UserRole + 1
_NAME_ROLE = Qt.UserRole + 2
_PROG_ROLE = Qt.UserRole + 3      # how far through this page's decode

INK = "#d6dae0"
DIM = "#7b818b"
ACCENT = "#ffc247"
GOOD = "#5fc26e"
BAD = "#e06c6c"
TRACK = "#3a3e46"


class _RowDelegate(QStyledItemDelegate):
    """Paints one page: its name, its state, and a bar showing where it is.

    Drawn rather than built from widgets, because a folder can hold several
    hundred scans and that many live progress bars is its own slowdown.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.phase = 0.0

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), 30)

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        r = option.rect
        state = index.data(_STATE_ROLE) or QUEUED
        name = index.data(_NAME_ROLE) or ""

        text_rect = QRect(r.left() + 10, r.top() + 4, r.width() - 130, 16)
        fm = QFontMetrics(option.font)
        p.setPen(QColor(INK if state != QUEUED else DIM))
        p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(name, Qt.ElideMiddle, text_rect.width()))

        label, colour = {
            QUEUED: ("waiting", DIM),
            READING: ("reading…", ACCENT),
            READY: ("ready", GOOD),
            FAILED: ("unreadable", BAD),
        }[state]
        if state == READING:
            frac = index.data(_PROG_ROLE)
            if frac is not None:
                label = f"{float(frac) * 100:.0f}%"
        p.setPen(QColor(colour))
        p.drawText(QRect(r.right() - 110, r.top() + 4, 100, 16),
                   Qt.AlignRight | Qt.AlignVCenter, label)

        # the bar
        bar = QRect(r.left() + 10, r.top() + 21, r.width() - 20, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(TRACK))
        p.drawRoundedRect(bar, 2, 2)
        if state == READY:
            p.setBrush(QColor(GOOD))
            p.drawRoundedRect(bar, 2, 2)
        elif state == FAILED:
            p.setBrush(QColor(BAD))
            p.drawRoundedRect(bar, 2, 2)
        elif state == READING:
            # A real fraction: the decoder works through the scan a tile at a
            # time and says how far it has got, so this fills rather than
            # merely animating.  Before the first tile lands there is nothing
            # to report yet, and a travelling segment says so honestly.
            frac = index.data(_PROG_ROLE)
            if frac is None:
                w = max(40, bar.width() // 5)
                span = bar.width() + w
                x = bar.left() - w + int((self.phase % 1.0) * span)
                seg = QRect(max(bar.left(), x), bar.top(),
                            min(w, bar.right() - max(bar.left(), x)), bar.height())
                if seg.width() > 0:
                    p.setBrush(QColor(ACCENT))
                    p.drawRoundedRect(seg, 2, 2)
            else:
                filled = QRect(bar.left(), bar.top(),
                               max(2, int(bar.width() * float(frac))), bar.height())
                p.setBrush(QColor(ACCENT))
                p.drawRoundedRect(filled, 2, 2)
        p.restore()


class QueueLoadDialog(QDialog):
    """Blocks the window until every page has been read once."""

    def __init__(self, pages: list, parent=None, is_ready=None):
        super().__init__(parent)
        # A second opinion on whether a page is finished, checked periodically.
        # The dialog cannot be closed by hand, so it must not be possible for a
        # missed notification to trap the window: if the proxy is on disk, the
        # page is done, whatever we were or were not told.
        self._is_ready = is_ready
        self.setWindowTitle("Preparing pages")
        self.setModal(True)
        # no close button, no system menu: there is deliberately no way out
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.resize(560, 460)

        self._rows: dict = {}
        self._done = 0
        self._finished = False
        # Counted by file, not by page.  The two halves of a cut scan and the
        # parts of a joined page share a source, and completions arrive per
        # path - so counting pages would leave a total that can never be
        # reached, behind a dialog with no close button.
        self._total = len({_norm(p.path) for p in pages})

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)

        self.title = QLabel(f"Reading {self._total} pages")
        f = self.title.font()
        f.setPointSize(12)
        f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)

        self.subtitle = QLabel(
            "Each scan is decoded once and cached, so this only happens the "
            "first time. Editing stays instant afterwards.")
        self.subtitle.setWordWrap(True)
        self.subtitle.setStyleSheet(f"color:{DIM};")
        v.addWidget(self.subtitle)

        self.bar = QProgressBar()
        self.bar.setMaximum(max(1, self._total))
        self.bar.setValue(0)
        self.bar.setFormat("%v of %m")
        v.addWidget(self.bar)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.delegate = _RowDelegate(self.list)
        self.list.setItemDelegate(self.delegate)
        self.list.setStyleSheet(
            "QListWidget{background:#212328;border:1px solid #34373d;}")
        for page in pages:
            key = _norm(page.path)
            if key in self._rows:
                continue
            item = QListWidgetItem()
            item.setData(_NAME_ROLE, os.path.basename(page.path))
            item.setData(_STATE_ROLE, QUEUED)
            self.list.addItem(item)
            self._rows[key] = item
        v.addWidget(self.list, 1)

        self.foot = QLabel("Please wait — the app is unavailable until this "
                           "finishes.")
        self.foot.setWordWrap(True)
        self.foot.setStyleSheet(f"color:{DIM};")
        v.addWidget(self.foot)

        # Hidden until progress has clearly stopped.  See the module docstring:
        # this is the failure exit, not a Cancel button.
        self.btn_give_up = QPushButton("Stop waiting and open what loaded")
        self.btn_give_up.setMinimumHeight(30)
        self.btn_give_up.clicked.connect(self._give_up)
        self.btn_give_up.hide()
        v.addWidget(self.btn_give_up)

        # Cancelling abandons the import outright - the pages go away again.
        # That is a different thing from the failure exit above, which keeps
        # whatever managed to load.
        self.btn_cancel = QPushButton("Cancel import")
        self.btn_cancel.setMinimumHeight(30)
        self.btn_cancel.clicked.connect(self._cancel)
        v.addWidget(self.btn_cancel)

        self._anim = QTimer(self)
        self._anim.setInterval(80)
        self._anim.timeout.connect(self._animate)
        self._anim.start()

        self._sweep = QTimer(self)
        self._sweep.setInterval(1500)
        self._sweep.timeout.connect(self._check_disk)
        self._sweep.start()

        self._sweep_at = 0            # where the bounded disk sweep is up to
        self._last_progress = time.monotonic()
        self._gave_up = False
        self._cancelled = False
        self.stall_seconds = 90.0

    # ------------------------------------------------------------- progress
    def set_phase(self, text: str) -> None:
        """What is happening right now - headers, then pixels.

        An import reads the file headers before it decodes anything, and that
        first step used to happen with the window live and no dialog on screen
        at all.  It is part of the wait, so it is shown as part of the wait.
        """
        self.title.setText(text)

    def mark_progress(self, path: str, fraction: float) -> None:
        """How far through this page's decode we are, as a real fraction."""
        item = self._rows.get(_norm(path))
        if item is None or item.data(_STATE_ROLE) in (READY, FAILED):
            return
        item.setData(_STATE_ROLE, READING)
        item.setData(_PROG_ROLE, max(0.0, min(1.0, float(fraction))))
        self._last_progress = time.monotonic()

    def mark_start(self, path: str) -> None:
        item = self._rows.get(_norm(path))
        if item is not None and item.data(_STATE_ROLE) == QUEUED:
            item.setData(_STATE_ROLE, READING)
            self._last_progress = time.monotonic()
            self.list.scrollToItem(item, QAbstractItemView.PositionAtCenter)

    def mark_done(self, path: str, ok: bool = True) -> None:
        item = self._rows.get(_norm(path))
        if item is None or item.data(_STATE_ROLE) in (READY, FAILED):
            return
        self._last_progress = time.monotonic()
        item.setData(_STATE_ROLE, READY if ok else FAILED)
        self._done += 1
        self.bar.setValue(self._done)
        left = self._total - self._done
        self.title.setText(f"Reading {self._total} pages — {left} to go"
                           if left else f"{self._total} pages ready")
        if self._done >= self._total:
            self.finish()

    def _check_disk(self) -> None:
        """Catch pages that finished without the news reaching us, and notice
        when nothing is happening at all.

        Two requests for the same scan are merged into one job, and a job can
        finish while another request for it is still being made.  Rather than
        try to make that unmissable, the dialog simply looks: the proxy is
        either on disk or it is not.
        """
        if self._finished:
            return
        if self._is_ready is not None:
            # Bounded, and picking up where it left off.  Each check is a
            # couple of stat calls on the interface thread, and scans usually
            # live on a network share where that is milliseconds each - a
            # sweep of six hundred rows every 1.5 s would itself be the stall
            # it is meant to detect.
            pending = [p for p, it in self._rows.items()
                       if it.data(_STATE_ROLE) not in (READY, FAILED)]
            if pending:
                self._sweep_at %= len(pending)
                window = pending[self._sweep_at:self._sweep_at + 40]
                self._sweep_at += len(window)
                for path in window:
                    try:
                        if self._is_ready(path):
                            self.mark_done(path, True)
                    except Exception:
                        pass
        self._check_stalled()

    def _check_stalled(self) -> None:
        """Offer a way out once progress has clearly stopped.

        The dialog cannot be dismissed by hand, which is only defensible while
        it is certain to finish.  Nothing that reads a disk is certain: a share
        can drop mid-import, a decoder can wedge, a notification can go astray.
        Rather than pretend otherwise and leave someone staring at a window
        that will never close, say plainly that it has stopped and let them out
        with whatever did load.
        """
        # isHidden, not isVisible: this runs before the dialog is on screen in
        # some paths, and a child of a hidden parent is never "visible"
        if self._finished or self._gave_up or not self.btn_give_up.isHidden():
            return
        if time.monotonic() - self._last_progress < self.stall_seconds:
            return
        left = self._total - self._done
        self.title.setText("This has stopped making progress")
        self.foot.setText(
            f"{left} page{'s' if left != 1 else ''} still to read, and nothing "
            f"has happened for {int(self.stall_seconds)} seconds. The scans may "
            f"be on a drive that has gone away, or one of them may be unreadable. "
            f"You can carry on with the pages that did load — the rest will be "
            f"read when you open them.")
        self.foot.setStyleSheet(f"color:{BAD};")
        self.btn_give_up.show()

    def _give_up(self) -> None:
        self._gave_up = True
        self.finish()

    def _cancel(self) -> None:
        self._cancelled = True
        self.finish()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def gave_up(self) -> bool:
        return self._gave_up

    def unread_paths(self) -> list:
        return [p for p, it in self._rows.items()
                if it.data(_STATE_ROLE) not in (READY, FAILED)]

    @property
    def is_finished(self) -> bool:
        """True once every page is accounted for.

        Named `is_finished` rather than `finished` because QDialog already has
        a `finished` signal, and a property cannot reliably shadow it - the
        attribute still resolves to the signal, which is always truthy.

        Callers must check this before exec().  Completion can happen *before*
        the loop starts - a page whose proxy landed between the caller listing
        it and requesting it reports back synchronously - and accept() with no
        loop running has nothing to exit, so exec() would then enter a loop
        that nothing could ever leave, behind a dialog with no close button.
        """
        return self._finished

    def finish(self) -> None:
        self._finished = True
        self._anim.stop()
        self._sweep.stop()
        self.accept()

    def _animate(self) -> None:
        self.delegate.phase += 0.06
        self.list.viewport().update()

    # --------------------------------------------------- no way out until done
    def reject(self) -> None:
        """Escape and the close button both land here. Ignore them."""
        if self._finished:
            super().reject()

    def closeEvent(self, ev):
        if self._finished:
            super().closeEvent(ev)
        else:
            ev.ignore()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            ev.ignore()
            return
        super().keyPressEvent(ev)
