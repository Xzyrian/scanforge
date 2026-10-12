"""Small building blocks shared across the interface."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (QHBoxLayout, QLayout, QToolButton, QWidget)

from . import icons


class FlowLayout(QLayout):
    """Lay items out in a row, wrapping to the next line when they run out.

    The control strip used to be a single row in a scroll area, which meant
    that in any window narrower than the whole strip the last controls in it -
    Revert, Join, Keep alignment, the ones that finish the job - were off the
    right-hand end behind a scrollbar.  Wrapping puts everything on screen at
    every width, and the strip's minimum width becomes its widest single item
    rather than the sum of all of them, so it still cannot shove the page grid
    aside the way the old row of group boxes did.
    """

    def __init__(self, parent=None, margin=0, hspace=6, vspace=6):
        super().__init__(parent)
        self._items = []
        self._hspace = hspace
        self._vspace = vspace
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):                             # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):                                 # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):                                 # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):                       # noqa: N802
        return Qt.Orientations(0)

    def hasHeightForWidth(self):                         # noqa: N802
        return True

    def heightForWidth(self, width):                     # noqa: N802
        return self._do(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect):                         # noqa: N802
        super().setGeometry(rect)
        self._do(rect, test=False)

    def sizeHint(self):                                  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):                               # noqa: N802
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect: QRect, test: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        # Lay out a line at a time, then centre each item on its line - a
        # label is shorter than the box beside it, and top-aligning them left
        # every label sitting a few pixels high.
        lines, line, x = [], [], eff.x()
        for it in self._items:
            if it.isEmpty():                 # a hidden widget takes no slot
                continue
            w = it.sizeHint().width()
            if line and x + w > eff.right() + 1:
                lines.append(line)
                line, x = [], eff.x()
            line.append(it)
            x += w + self._hspace
        if line:
            lines.append(line)
        y = eff.y()
        for line in lines:
            line_h = max(it.sizeHint().height() for it in line)
            if not test:
                x = eff.x()
                for it in line:
                    sh = it.sizeHint()
                    it.setGeometry(QRect(QPoint(x, y + (line_h - sh.height()) // 2),
                                         sh))
                    x += sh.width() + self._hspace
            y += line_h + self._vspace
        if lines:
            y -= self._vspace
        return y - rect.y() + m.bottom()


def _cluster(*widgets, spacing=4) -> QWidget:
    """A group of controls that wraps as one unit rather than splitting."""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for w in widgets:
        lay.addWidget(w)
    return box


def _sep() -> QWidget:
    """A hairline between two clusters.

    Painted as a coloured widget rather than a QFrame.VLine: the frame draws
    its line from the palette, so the stylesheet colour did not reach it and
    the separator came out invisible.  It also needs an explicit height, since
    a flow layout gives every item its own size hint and a bare line hints none.
    """
    line = QWidget()
    line.setFixedSize(1, 24)
    # a plain QWidget ignores a stylesheet background unless it is told to
    # draw one, which is why the first two attempts at this were invisible
    line.setAttribute(Qt.WA_StyledBackground, True)
    line.setStyleSheet("background-color:#454b55;")
    return line


def _tool(icon: str, tip: str, on_click=None, checkable=False) -> QToolButton:
    b = QToolButton()
    b.setIcon(icons.icon(icon, 18))
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.setCheckable(checkable)
    b.setFixedSize(28, 26)
    if on_click is not None:
        (b.toggled if checkable else b.clicked).connect(on_click)
    return b
