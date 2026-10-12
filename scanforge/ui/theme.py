"""One place for how ScanForge looks.

Colours used to be written out wherever they were needed - twenty-nine separate
style sheets, the same grey hint colour typed a dozen times and a few slightly
different greys that were meant to be the same one. They live here now, as
named tokens, with one application style sheet built from them.

Use `hint(label)` for secondary text rather than colouring it by hand, so the
whole app's secondary text stays one colour.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

# ------------------------------------------------------------------ tokens
WINDOW = "#2b2d32"        # window and dialog background
PANEL = "#2f3238"         # bars: top bar, options bar, tool column
BASE = "#212328"          # inputs and lists
CANVAS = "#26282c"        # behind the page
RAISED = "#35383f"        # buttons
RAISED_HOVER = "#3d4149"
BORDER = "#3a3d44"
BORDER_DARK = "#23252a"
TEXT = "#d6dae0"
HINT = "#9aa0aa"          # secondary text
FAINT = "#767c86"         # tertiary: units, scale marks
ACCENT = "#3a5f8a"        # selection, checked tools
ACCENT_LINE = "#4f7dae"
HIGHLIGHT = "#346fa8"
AMBER = "#ffc247"         # the app's mark; also "this edit reaches other pages"
DONE = "#5cbf73"


def palette() -> QPalette:
    p = QPalette()
    c = QColor
    p.setColor(QPalette.Window, c(WINDOW))
    p.setColor(QPalette.WindowText, c(TEXT))
    p.setColor(QPalette.Base, c(BASE))
    p.setColor(QPalette.AlternateBase, c("#30323a"))
    p.setColor(QPalette.Text, c(TEXT))
    p.setColor(QPalette.Button, c(RAISED))
    p.setColor(QPalette.ButtonText, c(TEXT))
    p.setColor(QPalette.Highlight, c(HIGHLIGHT))
    p.setColor(QPalette.HighlightedText, c("#ffffff"))
    p.setColor(QPalette.ToolTipBase, c("#282a2f"))
    p.setColor(QPalette.ToolTipText, c("#e2e6ec"))
    p.setColor(QPalette.PlaceholderText, c(FAINT))
    p.setColor(QPalette.Link, c("#7fb0e6"))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, c("#6c717a"))
    return p


STYLE = f"""
QToolTip {{
    background:#282a2f; color:#e2e6ec; border:1px solid {BORDER};
    padding:4px 6px;
}}
QPushButton {{
    background:{RAISED}; border:1px solid {BORDER}; border-radius:4px;
    padding:4px 12px; min-height:18px;
}}
QPushButton:hover {{ background:{RAISED_HOVER}; border-color:#4a4f58; }}
QPushButton:pressed {{ background:#2a2d33; }}
QPushButton:default {{ border-color:{ACCENT_LINE}; }}
QPushButton[primary="true"] {{
    background:{ACCENT}; border-color:{ACCENT_LINE}; color:#ffffff;
    font-weight:600; padding:7px 18px;
}}
QPushButton[primary="true"]:hover {{ background:#4270a0; }}
QPushButton[compact="true"] {{ padding:3px 4px; }}
QPushButton:disabled {{ color:#6c717a; background:#2f3137; border-color:#34373d; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background:{BASE}; border:1px solid {BORDER}; border-radius:3px;
    padding:2px 5px; min-height:18px;
    selection-background-color:{HIGHLIGHT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color:{ACCENT_LINE};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled {{ color:#6c717a; background:#26282d; }}
QGroupBox {{
    border:1px solid {BORDER}; border-radius:5px; margin-top:14px;
    padding:10px 8px 8px 8px;
}}
QGroupBox::title {{
    subcontrol-origin:margin; left:8px; padding:0 4px; color:{HINT};
}}
QMenu {{ background:#2c2e34; border:1px solid {BORDER}; padding:4px 0; }}
QMenu::item {{ padding:5px 26px 5px 22px; }}
QMenu::item:selected {{ background:{ACCENT}; }}
QMenu::separator {{ height:1px; background:{BORDER}; margin:4px 8px; }}
QMenuBar {{ background:{WINDOW}; }}
QMenuBar::item:selected {{ background:{RAISED_HOVER}; }}
QStatusBar {{ background:{PANEL}; border-top:1px solid {BORDER_DARK}; }}
QStatusBar::item {{ border:0; }}
QSplitter::handle {{ background:{WINDOW}; }}
QSplitter::handle:hover {{ background:#3a4556; }}
QScrollBar:vertical {{ background:transparent; width:11px; margin:0; }}
QScrollBar:horizontal {{ background:transparent; height:11px; margin:0; }}
QScrollBar::handle {{ background:#474b54; border-radius:4px; min-height:24px;
                      min-width:24px; margin:2px; }}
QScrollBar::handle:hover {{ background:#5a5f69; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}
QLabel[hint="true"] {{ color:{HINT}; }}
QLabel[faint="true"] {{ color:{FAINT}; }}
QLabel[heading="true"] {{ color:#e6e9ee; font-weight:600; }}
"""


def apply(app: QApplication) -> None:
    """Fusion, the palette and the style sheet: the whole look in one call."""
    app.setStyle("Fusion")
    app.setPalette(palette())
    app.setStyleSheet(STYLE)


def _mark(w: QWidget, prop: str) -> QWidget:
    w.setProperty(prop, True)
    w.style().unpolish(w)
    w.style().polish(w)
    return w


def hint(w: QWidget) -> QWidget:
    """Secondary text - explanations, readouts, anything read second."""
    return _mark(w, "hint")


def faint(w: QWidget) -> QWidget:
    """Tertiary text - units and scale marks."""
    return _mark(w, "faint")


def heading(w: QWidget) -> QWidget:
    return _mark(w, "heading")


def compact(button: QWidget) -> QWidget:
    """A small fixed-width button - "H", "0°" - that the normal padding would
    squeeze to nothing."""
    return _mark(button, "compact")


def primary(button: QWidget) -> QWidget:
    """The one button on a screen that most people want to press."""
    return _mark(button, "primary")
