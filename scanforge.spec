# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ScanForge.

Build with:  build-exe.bat        (or: pyinstaller scanforge.spec --noconfirm)

Two things matter here.  First, imagecodecs is a pile of separate C extensions
- one per compression format - and PyInstaller only sees the ones that happen
to be imported at analysis time, so they are collected explicitly; miss them
and the app runs fine until someone picks LZW in the export dialog.  Second,
PySide6 ships an enormous amount the app never touches (a whole web engine, a
QML runtime, 3D), and excluding it is the difference between a ~150 MB folder
and a ~500 MB one.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.getcwd())
ONEFILE = os.environ.get("SCANFORGE_ONEFILE", "") == "1"
ICON = os.path.join(ROOT, "tools", "scanforge.ico")

hiddenimports = []
binaries = []

# every codec extension, not just the ones imported during analysis
hiddenimports += collect_submodules("imagecodecs")
binaries += collect_dynamic_libs("imagecodecs")
hiddenimports += ["tifffile", "PIL.Image", "PIL.ImageFilter", "PIL.ImageCms"]

# Pillow registers its codecs lazily, by importing every *ImagePlugin module on
# first use.  Frozen, the ones nothing imports directly are simply absent, and
# saving a PDF then dies with KeyError: 'JPEG' because the PDF writer reaches
# for the JPEG encoder that was never registered.
hiddenimports += [
    "PIL.JpegImagePlugin", "PIL.PngImagePlugin", "PIL.TiffImagePlugin",
    "PIL.PdfImagePlugin", "PIL.BmpImagePlugin", "PIL.WebPImagePlugin",
    "PIL.Jpeg2KImagePlugin", "PIL.PpmImagePlugin", "PIL.GifImagePlugin",
    "PIL.IcoImagePlugin", "PIL._imaging", "PIL._imagingcms",
]

excludes = [
    # never used by this app, and large
    "tkinter", "matplotlib", "scipy", "pandas", "IPython", "notebook",
    "pytest", "setuptools", "pip", "wheel", "sqlite3", "unittest",
    # tifffile reaches for lxml only for OME-XML metadata and falls back to
    # the standard library without it; that is 9 MB for nothing here
    "lxml", "lxml.etree", "lxml._elementpath",
    "zmq", "tornado", "jinja2", "docutils", "pygments",
    # Qt modules the interface does not touch.  QtWebEngine alone is ~130 MB.
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtSerialBus", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtHelp", "PySide6.QtTest", "PySide6.QtSql",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtNetworkAuth",
    "PySide6.QtHttpServer", "PySide6.QtDBus",
]

datas = []
if os.path.exists(ICON):
    datas.append((ICON, "tools"))

a = Analysis(
    ["scanforge_app.py"],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_kwargs = dict(
    name="ScanForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                       # UPX-packed Qt DLLs trip antivirus
    console=False,                   # a GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if os.path.exists(ICON) else None,
)

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
              exclude_binaries=False, runtime_tmpdir=None, **exe_kwargs)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
                   name="ScanForge")
