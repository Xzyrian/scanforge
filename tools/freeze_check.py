"""Self-test that runs inside the built executable.

A frozen build fails in a very particular way: everything imports fine from
source, then the .exe starts and some C extension that PyInstaller did not
notice is missing.  Nothing catches that except running the real binary, so the
build script does exactly that - `ScanForge.exe --selftest` - and refuses to
call the build good until it passes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CHECKS = []
LINES: list[str] = []


def log_path() -> str:
    """Where the report is written.

    A windowed PyInstaller build has no stdout at all - `print` silently goes
    nowhere - so the one moment the report matters most, running the real .exe,
    is the moment it would be invisible.  Everything is therefore also written
    to a file the build script prints afterwards.
    """
    override = os.environ.get("SCANFORGE_SELFTEST_LOG")
    if override:
        return os.path.abspath(override)
    return os.path.join(tempfile.gettempdir(), "scanforge-selftest.log")


def say(line: str = "") -> None:
    LINES.append(line)
    try:
        print(line)
    except Exception:                                   # noqa: BLE001
        pass


def _flush_log() -> str:
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write("\n".join(LINES) + "\n")
    except Exception as exc:                            # noqa: BLE001
        try:
            print(f"(could not write {path}: {exc})")
        except Exception:                               # noqa: BLE001
            pass
    return path


def check(name: str, fn) -> bool:
    try:
        detail = fn() or ""
    except Exception as exc:                            # noqa: BLE001
        say(f"  [FAIL] {name}: {exc}")
        for chunk in traceback.format_exc().splitlines():
            say(f"         {chunk}")
        CHECKS.append(False)
        return False
    say(f"  [ok]   {name}" + (f"  - {detail}" if detail else ""))
    CHECKS.append(True)
    return True


def _numpy():
    import numpy as np
    return np.__version__


def _cv2():
    import cv2
    import numpy as np
    a = (np.random.default_rng(0).random((64, 64, 3)) * 255).astype(np.uint8)
    cv2.warpAffine(a, cv2.getRotationMatrix2D((32, 32), 1.0, 1.0), (64, 64))
    cv2.GaussianBlur(a, (5, 5), 0)
    return f"OpenCV {cv2.__version__}"


def _tifffile():
    import tempfile
    import numpy as np
    import tifffile
    from scanforge import imageio_util as iio
    a = (np.random.default_rng(1).random((80, 60, 3)) * 65535).astype(np.uint16)
    path = os.path.join(tempfile.mkdtemp(), "probe.tif")
    # exercise every codec the export dialog offers, since each one is a
    # separate imagecodecs extension that a frozen build can miss
    used = []
    for comp in ("none", "lzw", "zlib", "packbits"):
        iio.write_tiff(path, a, dpi=600, compression=None if comp == "none" else comp)
        back = iio.read_array(path)
        assert back.shape == a.shape, f"{comp}: {back.shape}"
        assert iio.probe(path)["dtype"] == "uint16", comp
        used.append(comp)
    return f"tifffile {tifffile.__version__}, codecs: {', '.join(used)}"


def _pillow():
    from PIL import Image, __version__
    Image.new("RGB", (8, 8)).convert("L")
    return f"Pillow {__version__}"


def _pdf():
    import tempfile
    import numpy as np
    from scanforge import imageio_util as iio
    from scanforge.export import build_pdf
    d = tempfile.mkdtemp()
    files = []
    for i in range(2):
        p = os.path.join(d, f"p{i}.tif")
        iio.write_tiff(p, np.full((120, 90, 3), 200, np.uint8), dpi=600)
        files.append(p)
    out = build_pdf(files, os.path.join(d, "out.pdf"), dpi=150)
    assert os.path.getsize(out) > 500, "pdf too small"
    return f"{os.path.getsize(out) / 1024:.0f} KB from 2 pages"


def _render():
    import numpy as np
    from scanforge import render
    from scanforge.geometry import Rect
    from scanforge.model import PageEdit
    src = (np.random.default_rng(2).random((600, 500, 3)) * 255).astype(np.uint8)
    e = PageEdit(path="x", src_w=500, src_h=600, angle=0.37)
    e.set_crop_rect(Rect(40, 50, 300, 400))
    e.blurs = [[80.0, 90.0, 120.0, 100.0, 20.0, "Blur"]]
    out = render.render(e, src)
    assert out.shape[:2] == (400, 300), str(out.shape)
    return "crop + rotate + blur"


def _join():
    """A joined page reads its parts itself, so it is its own code path."""
    import tempfile
    import numpy as np
    from scanforge import imageio_util as iio, render
    from scanforge.model import PageEdit

    d = tempfile.mkdtemp(prefix="sf_join_")
    paths = []
    for i, level in enumerate((80, 200)):
        p = os.path.join(d, f"half_{i}.tif")
        iio.write_tiff(p, np.full((300, 200, 3), level, np.uint8), dpi=600,
                       compression="zlib")
        paths.append(p)
    parts = [PageEdit(path=p, src_w=200, src_h=300, dpi=600.0) for p in paths]
    page = PageEdit.joined(parts, axis="h", gap=0)
    out = render.render(page)
    assert out.shape[:2] == (300, 400), str(out.shape)
    assert abs(int(out[:, :200].mean()) - 80) < 2, "left half wrong"
    return "two scans assembled into one page"


def _dialogs():
    """Modules only imported when a menu item is used - exactly the ones a
    frozen build is most likely to have left out."""
    from scanforge.ui.size_match_dialog import SizeMatchDialog
    from scanforge.ui.loading_dialog import QueueLoadDialog
    from scanforge.ui.export_dialog import ExportDialog
    from scanforge.ui.book_view import BookWindow, spreads_for
    assert spreads_for(5) == [(None, 0), (1, 2), (3, 4)]
    return "lazily-imported dialogs are present"


def _detect():
    import numpy as np
    from scanforge import autodetect
    g = np.full((400, 300, 3), 240, np.uint8)
    g[60:340, 40:260] = 250
    for y in range(80, 320, 14):
        g[y:y + 5, 60:240] = 30
    angle = autodetect.detect_skew(g)
    return f"deskew returned {angle:+.3f} deg"


def _qt():
    from PySide6 import __version__
    from PySide6.QtWidgets import QApplication
    from scanforge.ui.main_window import MainWindow, dark_palette
    from scanforge.ui import icons
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    bad = [n for n in icons._DRAW if icons.pixmap(n, 22).isNull()]
    assert not bad, f"icons failed to draw: {bad}"
    win = MainWindow()
    win.resize(900, 600)
    win.show()
    app.processEvents()
    assert win.isVisible(), "window did not become visible"
    n_tools = len([a for a in win.toolbar_tools.actions() if not a.isSeparator()])
    win._dirty = False
    win.close()
    app.processEvents()
    return f"Qt {__version__}, window + {len(icons._DRAW)} icons + {n_tools} tools"


def selftest() -> int:
    frozen = getattr(sys, "frozen", False)
    say("=" * 66)
    say(f"ScanForge self-test  ({'frozen build' if frozen else 'source tree'})")
    say("=" * 66)
    say(f"  python {sys.version.split()[0]}   exe: {sys.executable}")

    try:
        check("numpy", _numpy)
        check("opencv", _cv2)
        check("tifffile + imagecodecs", _tifffile)
        check("pillow", _pillow)
        check("pdf assembly", _pdf)
        check("render pipeline", _render)
        check("joined pages", _join)
        check("page detection", _detect)
        check("Qt interface", _qt)
        check("dialog modules", _dialogs)
    except BaseException as exc:                        # noqa: BLE001
        say(f"  [ABORTED] {type(exc).__name__}: {exc}")
        for chunk in traceback.format_exc().splitlines():
            say(f"         {chunk}")
        CHECKS.append(False)

    ok = all(CHECKS) and CHECKS
    say("-" * 66)
    say("PASS - this build works" if ok
        else f"FAIL - {CHECKS.count(False)} of {len(CHECKS)} checks failed")
    say(f"report written to {log_path()}")
    _flush_log()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(selftest())
