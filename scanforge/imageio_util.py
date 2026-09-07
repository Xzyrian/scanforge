"""Reading and writing large scans without losing what makes them archival:
bit depth, channel count, DPI and ICC profile.
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np

try:
    import tifffile
except Exception:                                    # pragma: no cover
    tifffile = None

from PIL import Image, ImageCms  # noqa: F401

Image.MAX_IMAGE_PIXELS = None                        # these are meant to be huge

RASTER_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".jp2", ".bmp", ".webp"}


# --------------------------------------------------------------------------
# probing (cheap: header only)
# --------------------------------------------------------------------------

def probe(path: str) -> dict:
    """Read size / dpi / dtype / channels without decoding pixel data."""
    info = {"width": 0, "height": 0, "dpi": 600.0, "channels": 3, "dtype": "uint8"}
    ext = os.path.splitext(path)[1].lower()

    if tifffile is not None and ext in (".tif", ".tiff"):
        try:
            with tifffile.TiffFile(path) as tf:
                page = tf.pages[0]
                info["height"], info["width"] = int(page.imagelength), int(page.imagewidth)
                info["channels"] = int(page.samplesperpixel)
                info["dtype"] = str(page.dtype)
                info["dpi"] = _tiff_dpi(page)
            return info
        except Exception:
            pass

    with Image.open(path) as im:
        info["width"], info["height"] = im.size
        dpi = im.info.get("dpi")
        if dpi and dpi[0]:
            info["dpi"] = float(dpi[0])
        info["channels"] = len(im.getbands())
        info["dtype"] = "uint16" if im.mode in ("I;16", "I;16B", "I") else "uint8"
    return info


def _tiff_dpi(page) -> float:
    try:
        tags = page.tags
        xres = tags["XResolution"].value
        unit = tags["ResolutionUnit"].value if "ResolutionUnit" in tags else 2
        val = xres[0] / xres[1] if isinstance(xres, tuple) else float(xres)
        unit = int(getattr(unit, "value", unit))
        if unit == 3:                                # centimetres
            val *= 2.54
        return round(float(val), 3) or 600.0
    except Exception:
        return 600.0


# --------------------------------------------------------------------------
# full decode
# --------------------------------------------------------------------------

def read_array(path: str, threads: int = 1) -> np.ndarray:
    """Decode to a numpy array, shape (H, W) or (H, W, C), uint8 or uint16.

    `threads` is tifffile's own strip/tile decode pool.  It defaults to *one*
    on purpose: every caller here already runs several files at once, and
    letting each of those spin up its own pool on top was measurably slower -
    2.5x on a 140 MP Deflate scan - as well as multiplying peak memory by the
    number of nested threads.  Pass a larger number only when decoding a single
    file with nothing else running.
    """
    ext = os.path.splitext(path)[1].lower()
    if tifffile is not None and ext in (".tif", ".tiff"):
        try:
            arr = tifffile.imread(path, maxworkers=max(1, int(threads)))
            return _sanitise(arr)
        except TypeError:                    # very old tifffile
            try:
                arr = tifffile.imread(path)
                return _sanitise(arr)
            except Exception:
                pass
        except Exception:
            pass
    with Image.open(path) as im:
        if im.mode == "P":
            im = im.convert("RGB")
        arr = np.asarray(im)
    return _sanitise(arr)


def _sanitise(arr: np.ndarray) -> np.ndarray:
    # A TIFF written with planarconfig=separate stores each colour as its own
    # plane, and comes back as (samples, height, width).  Left alone, the three
    # planes are read as three rows of pixels: a 3000-row scan becomes a proxy
    # one pixel tall.  Scanner software that writes separate planes is
    # uncommon but it exists, and the failure is silent.
    if (arr.ndim == 3 and arr.shape[0] in (3, 4)
            and arr.shape[0] < arr.shape[1] and arr.shape[0] < arr.shape[2]):
        arr = np.moveaxis(arr, 0, -1)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    if arr.ndim == 3 and arr.shape[2] > 4:
        arr = arr[:, :, :3]
    if arr.dtype == np.uint32 or arr.dtype == np.int32:
        arr = (arr >> 8).astype(np.uint16)
    if arr.dtype in (np.float32, np.float64):
        arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    if arr.dtype == np.bool_:
        arr = (arr.astype(np.uint8) * 255)
    if arr.dtype not in (np.uint8, np.uint16):
        arr = arr.astype(np.uint16)
    return np.ascontiguousarray(arr)


def read_icc(path: str) -> Optional[bytes]:
    try:
        with Image.open(path) as im:
            return im.info.get("icc_profile")
    except Exception:
        return None


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

TIFF_COMPRESSIONS = {
    "None": None,
    "LZW": "lzw",
    "Deflate (ZIP)": "zlib",
    "JPEG (lossy)": "jpeg",
    "Packbits": "packbits",
}


def write_tiff(path: str, arr: np.ndarray, dpi: float,
               compression: str = "zlib", icc: Optional[bytes] = None,
               quality: int = 92) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    if tifffile is not None:
        photometric = "minisblack" if arr.ndim == 2 else (
            "rgb" if arr.shape[2] >= 3 else "minisblack")
        kw = dict(
            photometric=photometric,
            resolution=(dpi, dpi),
            resolutionunit="INCH",
            metadata=None,
        )
        if compression:
            kw["compression"] = compression
            if compression == "jpeg":
                kw["compressionargs"] = {"level": quality}
        if arr.ndim == 3 and arr.shape[2] == 4:
            kw["extrasamples"] = ("unassalpha",)
        # tiles keep very large files quick to open in other software
        if arr.shape[0] * arr.shape[1] > 30_000_000 and compression != "jpeg":
            kw["tile"] = (512, 512)
        try:
            tifffile.imwrite(path, arr, **kw)
            if icc:
                _attach_icc_pil(path, icc)
            return
        except Exception as exc:                       # noqa: BLE001
            # The Pillow fallback below always writes Deflate, so whatever was
            # asked for, the file ends up compressed differently.  That used to
            # happen silently, which meant picking LZW could quietly produce a
            # Deflate file and nothing would ever say so.  Still fall back -
            # a written file beats a failed export - but say what happened.
            import warnings
            warnings.warn(
                f"TIFF compression {compression!r} unavailable ({exc}); "
                f"writing {os.path.basename(path)} with Deflate instead. "
                f"Install the 'imagecodecs' package for the full set.",
                RuntimeWarning, stacklevel=2)

    _pil_save(path, arr, dpi, icc)


def _attach_icc_pil(path: str, icc: bytes) -> None:
    # tifffile has no ICC hook; only worth a rewrite for modest files
    try:
        if os.path.getsize(path) > 400 * 1024 * 1024:
            return
        with Image.open(path) as im:
            im.load()
            im.save(path, icc_profile=icc, compression="tiff_deflate")
    except Exception:
        pass


def _pil_save(path: str, arr: np.ndarray, dpi: float, icc: Optional[bytes]) -> None:
    im = to_pil(arr)
    kw = {"dpi": (dpi, dpi)}
    if icc:
        kw["icc_profile"] = icc
    ext = os.path.splitext(path)[1].lower()
    if ext in (".tif", ".tiff"):
        kw["compression"] = "tiff_deflate"
    im.save(path, **kw)


def write_image(path: str, arr: np.ndarray, dpi: float, quality: int = 92,
                icc: Optional[bytes] = None, compression: str = "zlib") -> None:
    ext = os.path.splitext(path)[1].lower()
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if ext in (".tif", ".tiff"):
        write_tiff(path, arr, dpi, compression, icc, quality)
        return
    im = to_pil(arr)
    kw = {"dpi": (dpi, dpi)}
    if icc:
        kw["icc_profile"] = icc
    if ext in (".jpg", ".jpeg"):
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        kw.update(quality=quality, subsampling=0, optimize=True)
    im.save(path, **kw)


def to_pil(arr: np.ndarray) -> Image.Image:
    if arr.dtype == np.uint16:
        if arr.ndim == 2:
            return Image.fromarray(arr, mode="I;16")
        arr = (arr >> 8).astype(np.uint8)
    if arr.ndim == 2:
        return Image.fromarray(arr, mode="L")
    if arr.shape[2] == 3:
        return Image.fromarray(arr, mode="RGB")
    if arr.shape[2] == 4:
        return Image.fromarray(arr, mode="RGBA")
    return Image.fromarray(arr[:, :, 0], mode="L")


def to_8bit(arr: np.ndarray) -> np.ndarray:
    if arr.dtype == np.uint16:
        return (arr >> 8).astype(np.uint8)
    return arr
