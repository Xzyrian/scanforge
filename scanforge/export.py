"""Batch export.

Runs on a small pool of worker threads.  OpenCV's warp, numpy's copies and
imagecodecs' compressors all release the GIL, so threads genuinely run in
parallel here while keeping the process model simple - no Windows spawn
weirdness, and cancelling actually works.
"""

from __future__ import annotations

import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event
from typing import Callable, Optional

import numpy as np

from . import imageio_util as iio
from . import render as rnd
from .model import PageEdit


@dataclass
class ExportSettings:
    out_dir: str = ""
    fmt: str = "TIFF"                      # TIFF | JPEG | PNG
    compression: str = "None"              # TIFF only; archival masters first
    jpeg_quality: int = 92
    bit_depth: str = "Same as source"      # Same as source | 8-bit
    resample: str = "Lanczos (sharpest)"
    scale_mode: str = "Full resolution"    # Full resolution | Target DPI | Percent
    target_dpi: float = 300.0
    percent: float = 100.0
    fill: int = 255                        # colour exposed by rotation
    document: str = ""                     # one name for the whole set
    naming: str = "{doc}-{page}"           # {doc} {page} {name} {index}
    digits: int = 4                        # width of {page}, e.g. 4 -> 0001
    start_number: int = 1                  # {page} of the first page in a document
    subfolder_per_doc: bool = False
    overwrite: bool = False
    zip_images: bool = True                # <document>-images.zip beside them
    make_pdf: bool = False
    pdf_dpi: float = 0.0                   # 0 means "match the source scans"
    pdf_quality: int = 88
    workers: int = 2
    also_jpeg_preview: bool = False
    preview_long_edge: int = 2000


@dataclass
class ExportResult:
    uid: str
    ok: bool
    path: str = ""
    error: str = ""
    seconds: float = 0.0
    size_bytes: int = 0


_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(text: str) -> str:
    return _SAFE.sub("_", text).strip().rstrip(".") or "untitled"


NAME_PRESETS = {
    "Document name + page number (dash)": "{doc}-{page}",
    "Document name + page number": "{doc}_{page}",
    "Page number only": "{page}",
    "Original file name": "{name}",
    "Original file name + page number": "{name}_{page}",
}


def format_name(cfg: ExportSettings, doc: str, stem: str, index: int) -> str:
    """Resolve the naming pattern for one page.

    `index` is 1-based within its document; {page} then applies the start
    number and zero padding, so the first page of a document is 0001 by
    default rather than 0000.
    """
    number = cfg.start_number + index - 1
    digits = max(1, min(9, cfg.digits))
    return cfg.naming.format(
        doc=doc or "page",
        name=stem,
        index=number,
        page=f"{number:0{digits}d}",
        # kept working for projects saved with the earlier tokens
        n2=f"{number:02d}", n3=f"{number:03d}", n4=f"{number:04d}",
    )


def output_path(edit: PageEdit, cfg: ExportSettings, index: int) -> str:
    stem = os.path.splitext(os.path.basename(edit.path))[0]
    if edit.label:
        # two halves of a cut scan share a source file, so a name-based scheme
        # would write them both to one path and silently keep only the second
        stem = f"{stem}_{edit.label.replace('/', '-').replace(' ', '-')}"
    doc = cfg.document or ""
    name = format_name(cfg, doc, stem, index)
    ext = {"TIFF": ".tif", "JPEG": ".jpg", "PNG": ".png"}[cfg.fmt]
    folder = cfg.out_dir
    if cfg.subfolder_per_doc and doc:
        folder = os.path.join(folder, _safe(doc))
    return os.path.join(folder, _safe(name) + ext)


def pdf_path(cfg: ExportSettings, doc: str = "") -> str:
    """The PDF takes the document name alone - same stem, no page number."""
    return os.path.join(cfg.out_dir, _safe(doc or cfg.document or "pages") + ".pdf")


def resolve_pdf_dpi(cfg: ExportSettings, edits: list) -> float:
    """PDF resolution, defaulting to whatever the scans themselves are."""
    if cfg.pdf_dpi and cfg.pdf_dpi > 0:
        return cfg.pdf_dpi
    dpis = [e.dpi for e in edits if e.dpi]
    if not dpis:
        return 300.0
    dpis.sort()
    return float(dpis[len(dpis) // 2])


def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


def _scale_for(edit: PageEdit, cfg: ExportSettings) -> float:
    if cfg.scale_mode == "Percent":
        return max(0.01, cfg.percent / 100.0)
    if cfg.scale_mode == "Target DPI":
        src = edit.dpi or 600.0
        return max(0.01, min(1.0, cfg.target_dpi / src))
    return 1.0


def export_one(edit: PageEdit, cfg: ExportSettings, index: int,
               cancel: Optional[Event] = None) -> ExportResult:
    t0 = time.time()
    try:
        if cancel is not None and cancel.is_set():
            return ExportResult(edit.uid, False, error="cancelled")

        scale = _scale_for(edit, cfg)
        interp = rnd.RESAMPLE.get(cfg.resample, "lanczos")

        if edit.join:
            # a page assembled from several scans: render() reads the parts
            # itself, because there is no single source file to hand it
            out = rnd.render(edit, interp=interp, fill=cfg.fill,
                             output_scale=scale)
        else:
            arr = iio.read_array(edit.path)
            out = rnd.render(edit, arr, interp=interp, fill=cfg.fill,
                             output_scale=scale)
            del arr

        if cfg.bit_depth == "8-bit":
            out = iio.to_8bit(out)

        dpi = (edit.dpi or 600.0) * scale
        dest = output_path(edit, cfg, index)
        if not cfg.overwrite:
            dest = _unique(dest)

        iio.write_image(dest, out, dpi=dpi, quality=cfg.jpeg_quality,
                        compression=iio.TIFF_COMPRESSIONS.get(cfg.compression, "zlib"))

        if cfg.also_jpeg_preview and cfg.fmt == "TIFF":
            _write_preview(dest, out, dpi, cfg)

        del out
        size = os.path.getsize(dest) if os.path.exists(dest) else 0
        return ExportResult(edit.uid, True, dest, seconds=time.time() - t0,
                            size_bytes=size)
    except Exception as exc:                          # noqa: BLE001
        return ExportResult(edit.uid, False,
                            error=f"{exc}\n{traceback.format_exc(limit=3)}",
                            seconds=time.time() - t0)


def _write_preview(dest: str, arr: np.ndarray, dpi: float, cfg: ExportSettings) -> None:
    h, w = arr.shape[:2]
    s = cfg.preview_long_edge / float(max(w, h))
    small = rnd._resize(arr, s, "lanczos") if s < 1.0 else arr
    folder = os.path.join(os.path.dirname(dest), "preview")
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, os.path.splitext(os.path.basename(dest))[0] + ".jpg")
    iio.write_image(out, iio.to_8bit(small), dpi=dpi * (s if s < 1 else 1),
                    quality=88)


# --------------------------------------------------------------------------
# PDF assembly
# --------------------------------------------------------------------------

def build_pdf(files: list, out_path: str, dpi: float = 200.0,
              quality: int = 88, cancel: Optional[Event] = None) -> str:
    from PIL import Image
    # Pillow's PDF writer reaches into Image.SAVE["JPEG"] directly, and that
    # entry only exists once JpegImagePlugin has been imported.  Pillow does
    # that lazily by scanning its own package directory, which finds nothing
    # inside a frozen build - so import it explicitly and fail loudly rather
    # than with a bare KeyError halfway through assembling a PDF.
    try:
        from PIL import JpegImagePlugin, PdfImagePlugin  # noqa: F401
    except ImportError as exc:                     # pragma: no cover
        raise RuntimeError(
            f"Pillow's JPEG support is missing from this build ({exc}); "
            f"PDF assembly needs it.") from exc
    pages = []
    try:
        for f in files:
            if cancel is not None and cancel.is_set():
                return ""
            arr = iio.read_array(f)
            arr = iio.to_8bit(arr)
            info = iio.probe(f)
            s = dpi / max(1.0, info.get("dpi", 600.0))
            if s < 1.0:
                arr = rnd._resize(arr, s, "lanczos")
            im = iio.to_pil(arr)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            pages.append(im)
        if not pages:
            return ""
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        pages[0].save(out_path, save_all=True, append_images=pages[1:],
                      resolution=dpi, quality=quality)
        return out_path
    finally:
        for im in pages:
            try:
                im.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# the batch runner
# --------------------------------------------------------------------------

class BatchExporter:
    """Plain-Python runner; the UI wraps it in a QThread and forwards callbacks."""

    def __init__(self, edits: list, cfg: ExportSettings):
        self.edits = list(edits)
        self.cfg = cfg
        self.cancel = Event()
        self.on_progress: Optional[Callable[[int, int, ExportResult], None]] = None
        self.on_message: Optional[Callable[[str], None]] = None
        self.results: list = []

    def stop(self) -> None:
        self.cancel.set()

    def run(self) -> list:
        cfg = self.cfg
        total = len(self.edits)
        done = 0

        # one document, so page numbers simply run 1..n in queue order
        jobs = [(e, i + 1) for i, e in enumerate(self.edits)]

        from concurrent.futures import as_completed
        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
            futures = {pool.submit(export_one, e, cfg, i, self.cancel): e
                       for e, i in jobs}
            for fut in as_completed(futures):
                res = fut.result()
                self.results.append(res)
                done += 1
                if self.on_progress:
                    self.on_progress(done, total, res)
                if self.cancel.is_set():
                    for f in futures:
                        f.cancel()
                    break

        if cfg.zip_images and not self.cancel.is_set():
            self._zip_images()
        if cfg.make_pdf and not self.cancel.is_set():
            self._build_pdfs()
        return self.results

    def _zip_images(self) -> None:
        """Bundle the exported pages for sending. The PDF stays out of it."""
        import zipfile
        order = {e.uid: i for i, e in enumerate(self.edits)}
        files = sorted(((order.get(r.uid, 0), r.path) for r in self.results
                        if r.ok and os.path.exists(r.path)))
        if not files:
            return
        out = os.path.join(self.cfg.out_dir,
                           _safe(self.cfg.document or "pages") + "-images.zip")
        if self.on_message:
            self.on_message(f"Packing {len(files)} pages into "
                            f"{os.path.basename(out)}...")
        try:
            # level 1: uncompressed TIFFs squeeze down a long way even so, and
            # the alternative is spending minutes on a marginal size gain
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=1) as z:
                for _i, f in files:
                    if self.cancel.is_set():
                        return
                    z.write(f, os.path.basename(f))
            if self.on_message:
                self.on_message(f"Wrote {out} "
                                f"({os.path.getsize(out) / 1e6:.0f} MB)")
        except Exception as exc:                       # noqa: BLE001
            if self.on_message:
                self.on_message(f"Could not write the zip: {exc}")

    def _build_pdfs(self) -> None:
        order = {e.uid: i for i, e in enumerate(self.edits)}
        done = sorted(((order.get(r.uid, 0), r.path)
                       for r in self.results if r.ok))
        files = [p for _, p in done]
        if not files or self.cancel.is_set():
            return
        out = pdf_path(self.cfg)
        dpi = resolve_pdf_dpi(self.cfg, self.edits)
        if self.on_message:
            self.on_message(f"Assembling {os.path.basename(out)} "
                            f"({len(files)} pages at {dpi:g} dpi)...")
        try:
            build_pdf(files, out, dpi, self.cfg.pdf_quality, self.cancel)
            if self.on_message:
                self.on_message(f"Wrote {out}")
        except Exception as exc:                      # noqa: BLE001
            if self.on_message:
                self.on_message(f"PDF failed: {exc}")
