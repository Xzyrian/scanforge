"""Entry point:  python -m scanforge  [files or folders...]"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # Qt reads these before QApplication exists
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui import icons, theme

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("ScanForge")
    app.setOrganizationName("ScanForge")
    theme.apply(app)
    app.setWindowIcon(icons.app_icon())

    win = MainWindow()
    win.show()

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        from . import imageio_util as iio
        paths = []
        for a in args:
            if os.path.isdir(a):
                for root, _d, files in os.walk(a):
                    for f in sorted(files):
                        if os.path.splitext(f)[1].lower() in iio.RASTER_EXTS:
                            paths.append(os.path.join(root, f))
            elif os.path.isfile(a):
                paths.append(a)
        if paths:
            win.add_paths(paths)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
