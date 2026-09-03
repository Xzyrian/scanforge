"""Things that tell you the app is working.

The complaint these answer is not really "it is slow" - decoding a folder of
140-megapixel TIFFs takes as long as it takes - but "it went quiet and I could
not tell whether it was working or wedged".  So: a spinner that only spins when
something is genuinely running, a panel over the canvas saying what is being
waited on, and a progress bar for the one job that takes minutes.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

ACCENT = "#ffc247"


class Throbber(QWidget):
    """A small spinning arc. Costs nothing when stopped, because it stops."""

    def __init__(self, size: int = 16, parent=None):
        super().__init__(parent)
        self._size = size
        self._angle = 0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.show()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    @property
    def spinning(self) -> bool:
        return self._timer.isActive()

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self._size
        pen = QPen(QColor(ACCENT), max(2.0, w / 8.0))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        inset = pen.widthF()
        p.drawArc(QRectF(inset, inset, w - 2 * inset, w - 2 * inset),
                  -self._angle * 16, 110 * 16)
        pen.setColor(QColor(255, 194, 71, 60))
        p.setPen(pen)
        p.drawArc(QRectF(inset, inset, w - 2 * inset, w - 2 * inset),
                  (-self._angle + 130) * 16, 210 * 16)
        p.end()


class CanvasNotice(QWidget):
    """A panel floated over the canvas: a message, a spinner, some buttons.

    Used both for "this page is decoding, hold on" and for modes that need an
    instruction and a way out, like picking the facing page of a spread.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "CanvasNotice{background:#2b2e35;border:1px solid #4a4f58;"
            "border-radius:10px;}"
            "QLabel#title{color:#eceff4;font-size:15px;font-weight:600;}"
            "QLabel#body{color:#aeb4bd;font-size:12px;}")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.throbber = Throbber(18, self)
        head.addWidget(self.throbber, 0, Qt.AlignVCenter)
        self.lbl_title = QLabel("", self)
        self.lbl_title.setObjectName("title")
        head.addWidget(self.lbl_title, 1)
        root.addLayout(head)

        self.lbl_body = QLabel("", self)
        self.lbl_body.setObjectName("body")
        self.lbl_body.setWordWrap(True)
        root.addWidget(self.lbl_body)

        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        self.buttons.addStretch(1)
        root.addLayout(self.buttons)

        self._btns: list = []
        self.hide()

    # ------------------------------------------------------------------ show
    def show_notice(self, title: str, body: str = "", spinning: bool = False,
                    actions: Optional[list] = None) -> None:
        self.lbl_title.setText(title)
        self.lbl_body.setText(body)
        self.lbl_body.setVisible(bool(body))
        self.throbber.start() if spinning else self.throbber.stop()

        for b in self._btns:
            self.buttons.removeWidget(b)
            b.deleteLater()
        self._btns = []
        for label, slot in (actions or []):
            b = QPushButton(label, self)
            b.setMinimumHeight(28)
            b.clicked.connect(slot)
            self.buttons.addWidget(b)
            self._btns.append(b)
        self.buttons.parentWidget().setVisible(True) if self._btns else None

        self.adjustSize()
        self.setFixedWidth(min(460, max(300, self.parent().width() - 80)))
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    def dismiss(self) -> None:
        self.throbber.stop()
        self.hide()

    def reposition(self) -> None:
        par = self.parentWidget()
        if par is None:
            return
        self.move(max(0, (par.width() - self.width()) // 2),
                  max(0, (par.height() - self.height()) // 2))
