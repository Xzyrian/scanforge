"""Proxy pyramid + disk cache.

The whole reason GIMP feels like wading through treacle on a 140-megapixel scan
is that it wants to touch every pixel for every interaction.  Here the source
file is decoded exactly once, three reduced copies are written to a cache
folder, and all interactive work happens on those.  Full resolution is only
ever touched again at export.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import numpy as np

try:
    import cv2
except Exception:                                    # pragma: no cover
    cv2 = None

from PIL import Image

from . import imageio_util as iio

# long-edge pixel budget for each tier
THUMB = 320
PROXY = 2200      # what you edit against; ~1570x2200 for a 600dpi page
DETAIL = 5200     # swapped in when you zoom past ~40%

_TIERS = {"thumb": THUMB, "proxy": PROXY, "detail": DETAIL}

# How much of the progress bar the decode owns.  The rest belongs to shrinking
# and writing the tiers, which is a real part of the wait - half a second a page
# for three tiers of JPEG - and used to happen with the bar sitting on 100%.
_DECODE_SHARE = 0.88

# Why the last decode took the path it did.  Kept for the diagnostic in
# `tools/decode_report.py`; nothing in the app reads it.
last_strategy: dict = {}


def _note(path: str, strategy: str, detail: str = "") -> None:
    last_strategy[os.path.basename(path)] = (strategy, detail)
    if os.environ.get("SCANFORGE_DEBUG"):
        import sys
        print(f"[proxycache] {os.path.basename(path)}: {strategy}"
              + (f" ({detail})" if detail else ""), file=sys.stderr)


def cache_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "ScanForge", "proxies")
    os.makedirs(path, exist_ok=True)
    return path


def _key(path: str) -> str:
    try:
        st = os.stat(path)
        sig = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        sig = os.path.abspath(path)
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:20]


def _cache_file(path: str, tier: str) -> str:
    return os.path.join(cache_root(), f"{_key(path)}_{tier}.jpg")


def _meta_file(path: str) -> str:
    return os.path.join(cache_root(), f"{_key(path)}.json")


# --------------------------------------------------------------------------
# downsampling
# --------------------------------------------------------------------------

def _layout(path: str) -> Optional[dict]:
    """What the TIFF is made of, in one open, or None if it is not one we can
    walk segment by segment."""
    if iio.tifffile is None or cv2 is None:
        return None
    if not path.lower().endswith((".tif", ".tiff")):
        return None
    try:
        with iio.tifffile.TiffFile(path) as tf:
            page = tf.pages[0]
            shape = page.shape
            if len(shape) not in (2, 3):
                return None
            H, W = int(shape[0]), int(shape[1])
            C = int(shape[2]) if len(shape) == 3 else 1
            if C > 4 or not H or not W:
                return None
            if page.dtype not in (np.uint8, np.uint16):
                return None
            if int(getattr(page, "planarconfig", 1) or 1) != 1:
                return None
            if not getattr(page, "chunks", None):
                return None
            try:
                comp = int(page.compression)
            except Exception:                            # noqa: BLE001
                comp = 0
            return {"H": H, "W": W, "C": C, "dtype": page.dtype,
                    "segments": max(1, len(getattr(page, "dataoffsets", ()) or ())),
                    "stored": H * W * C * int(page.dtype.itemsize),
                    "raw": comp == 1}
    except Exception:                                    # noqa: BLE001
        return None


def _plan(path: str, long_edge: int, layout: Optional[dict]) -> str:
    """Which of the three decodes to use for this file.

    They are not interchangeable, and picking the wrong one is what made
    importing feel slower than it needed to.  Measured on a 108 MP page, warm
    cache, best of three:

        layout          accumulate      paste       whole
        tiled Deflate     0.97 s       0.65 s      0.66 s     312 / 566 / 565 MB
        striped Deflate   0.83 s       0.57 s      0.59 s     311 / 565 / 563 MB
        striped LZW       2.52 s       3.10 s      2.51 s     398 / 650 / 648 MB
        uncompressed      0.89 s       0.53 s      0.21 s     686 / 890 / 397 MB

    So: an uncompressed TIFF is read whole, because tifffile maps it straight
    off the disk - four times faster *and* smaller than walking it.  Anything
    compressed is walked segment by segment, which is what makes an honest
    percentage possible; whether each segment is pasted into a full-size array
    (fast) or averaged into the small one (about 45% less memory, about 40%
    slower) depends on whether the machine has the room to spare.
    """
    forced = os.environ.get("SCANFORGE_DECODE", "").strip().lower()
    if forced in ("whole", "paste", "accumulate"):
        return "whole" if layout is None else forced
    if layout is None:
        return "whole"
    if long_edge >= max(layout["W"], layout["H"]):
        return "whole"                        # already small; nothing to gain
    if layout["raw"]:
        return "whole"
    # Pasting is worth roughly 30% of the decode and costs an extra full-size
    # array - 320 MB for a 140 MP page, times however many decodes the gate is
    # letting through.  That is a good trade with memory to spare and a bad one
    # without, so the bar is set high: the array itself, the resize's working
    # copy, and room for the other decodes in flight, with headroom left over.
    return ("paste" if available_memory() > layout["stored"] * 3.5
            else "accumulate")


def _walk(path: str, long_edge: int, layout: dict, mode: str,
          on_progress=None) -> np.ndarray:
    """Decode a TIFF segment by segment and return it reduced to `long_edge`.

    Walking the file is what gives a real percentage: a 140 MP scan is a few
    hundred segments, so there is something honest to report several times a
    second rather than an animation that only means "still working".

    `mode` decides what happens to each segment as it arrives.  "paste" writes
    it into a full-size array and resizes once at the end.  "accumulate"
    reduces each segment on arrival and averages it into the small array, so
    the full-size array is never built at all.
    """
    H, W, C = layout["H"], layout["W"], layout["C"]
    scale = long_edge / float(max(W, H))
    oh, ow = max(1, int(round(H * scale))), max(1, int(round(W * scale)))
    total = layout["segments"]
    step = max(1, total // 60)                # ~60 reports per page is plenty

    if mode == "paste":
        full = np.empty((H, W, C), layout["dtype"])
    else:
        acc = np.zeros((oh, ow, C), np.float32)
        wgt = np.zeros((oh, ow, 1), np.float32)

    with iio.tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        for n, (data, index, sshape) in enumerate(page.segments(), 1):
            if on_progress is not None and (n % step == 0 or n == total):
                on_progress(min(1.0, n / total))
            if data is None:
                continue
            y0, x0 = int(index[2]), int(index[3])
            if y0 >= H or x0 >= W:
                continue
            blk = np.asarray(data).reshape(sshape)
            while blk.ndim > 3:
                blk = blk[0]
            if blk.ndim == 2:
                blk = blk[:, :, None]
            bh = min(blk.shape[0], H - y0)
            bw = min(blk.shape[1], W - x0)
            blk = blk[:bh, :bw]

            if mode == "paste":
                full[y0:y0 + bh, x0:x0 + bw] = blk
                continue

            dy0, dx0 = int(y0 * scale), int(x0 * scale)
            dy1 = min(oh, max(dy0 + 1, int(round((y0 + bh) * scale))))
            dx1 = min(ow, max(dx0 + 1, int(round((x0 + bw) * scale))))
            if dy1 <= dy0 or dx1 <= dx0:
                continue
            small = cv2.resize(blk, (dx1 - dx0, dy1 - dy0),
                               interpolation=cv2.INTER_AREA)
            if small.ndim == 2:
                small = small[:, :, None]
            acc[dy0:dy1, dx0:dx1] += small.astype(np.float32)
            wgt[dy0:dy1, dx0:dx1] += 1.0

    if mode == "paste":
        if C == 1:
            full = full[:, :, 0]
        elif C == 4:
            full = full[:, :, :3]
        return _shrink(full, long_edge)

    out = acc / np.maximum(wgt, 1e-6)
    if layout["dtype"] == np.uint16:
        out = out / 257.0
    out = np.clip(out, 0, 255).astype(np.uint8)
    if C == 1:
        out = out[:, :, 0]
    elif C == 4:
        out = out[:, :, :3]
    return np.ascontiguousarray(out)


def _decode(path: str, long_edge: int, on_progress=None) -> np.ndarray:
    """Get `path` down to `long_edge`, by whichever route suits the file.

    Every route ends in an array no larger than `long_edge`, so the caller can
    shrink the smaller tiers out of it without going back to the source.

    A failure part-way through a walk is not silently retried as if nothing had
    happened: it is recorded, and running out of memory in particular falls to
    the *smaller* decode rather than the whole-file one, which is the mistake
    that turns a tight machine into a stalled one.
    """
    layout = _layout(path)
    plan = _plan(path, long_edge, layout)

    if plan != "whole":
        try:
            out = _walk(path, long_edge, layout, plan, on_progress)
            _note(path, plan, f"{layout['segments']} segments")
            return out
        except MemoryError:
            # the answer to running out of room is never "now read the whole
            # file into memory instead"
            set_decode_limit(1)
            time.sleep(0.5)
            try:
                out = _walk(path, long_edge, layout, "accumulate", on_progress)
                _note(path, "accumulate", "after running short of memory")
                return out
            except Exception as exc:                     # noqa: BLE001
                _note(path, "whole", f"walk failed twice: {exc}")
        except Exception as exc:                         # noqa: BLE001
            _note(path, "whole", f"walk failed: {exc}")

    if on_progress is not None:
        on_progress(0.02)
    try:
        arr = iio.read_array(path)
    except MemoryError:
        # Running out of memory mid-import used to take the whole application
        # with it.  Drop to one decode at a time, let whatever else is in
        # flight finish and release its buffers, and try again: slow beats
        # dead.
        set_decode_limit(1)
        time.sleep(1.0)
        arr = iio.read_array(path)
    if plan == "whole" and layout is not None:
        _note(path, "whole", "raw" if layout["raw"] else "already small")
    elif plan == "whole":
        _note(path, "whole", "not a walkable TIFF")
    out = _shrink(arr, long_edge)
    del arr
    if on_progress is not None:
        on_progress(1.0)
    return out


def _shrink(arr: np.ndarray, long_edge: int) -> np.ndarray:
    """Reduce to `long_edge` and return 8-bit.

    The downsample happens at the source bit depth and the conversion to 8-bit
    afterwards, on the small array.  Doing it the other way round costs a
    full-size copy of a 140 MP scan for pixels that are about to be discarded.
    """
    h, w = arr.shape[:2]
    scale = long_edge / float(max(w, h))
    if scale >= 1.0:
        return iio.to_8bit(arr)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    if cv2 is not None:
        # two-stage: a cheap integer box reduction first, then a clean area
        # resample.  Roughly 3x faster than one huge INTER_AREA pass.
        f = int(max(1, min(w // max(nw, 1), h // max(nh, 1))))
        if f >= 2:
            f = min(f, 8)
            arr = cv2.resize(arr, (w // f, h // f), interpolation=cv2.INTER_AREA)
        return iio.to_8bit(cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_AREA))
    im = iio.to_pil(iio.to_8bit(arr))
    return np.asarray(im.resize((nw, nh), Image.LANCZOS))


def _read_reduced(path: str, long_edge: int) -> Optional[np.ndarray]:
    """Use an embedded pyramid level if the TIFF has one - near-instant."""
    if iio.tifffile is None or not path.lower().endswith((".tif", ".tiff")):
        return None
    try:
        with iio.tifffile.TiffFile(path) as tf:
            series = tf.series[0]
            levels = getattr(series, "levels", None)
            if not levels or len(levels) < 2:
                return None
            best = None
            for lv in levels:
                shape = lv.shape
                if len(shape) < 2:
                    continue
                le = max(shape[0], shape[1])
                if le >= long_edge and (best is None or le < best[0]):
                    best = (le, lv)
            if best is None:
                return None
            return iio._sanitise(best[1].asarray())
    except Exception:
        return None


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

class _DecodeGate:
    """Caps how many full-resolution decodes happen at once, app-wide.

    There are three separate thread pools that can each decide to decode a
    scan - the prefetcher warming the queue, the loader fetching the page you
    just clicked, and one-off background jobs - and none of them knew about the
    others.  Six concurrent 140 MP decodes is around six gigabytes of buffers,
    at which point the machine swaps and everything crawls.  One gate in front
    of the decode itself is the only place that can actually hold the line.
    """

    def __init__(self, limit: int):
        self._limit = max(1, limit)
        self._used = 0
        self._cv = threading.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    def set_limit(self, n: int) -> None:
        with self._cv:
            self._limit = max(1, int(n))
            self._cv.notify_all()

    def __enter__(self):
        with self._cv:
            while self._used >= self._limit:
                self._cv.wait()
            self._used += 1
        return self

    def __exit__(self, *exc):
        with self._cv:
            self._used -= 1
            self._cv.notify()
        return False


_gate = _DecodeGate(2)


def set_decode_limit(n: int) -> None:
    """Cap concurrent decodes app-wide.

    One is a legitimate answer and is honoured.  An earlier version forced a
    floor of two so a page you clicked could not queue behind a background
    decode - but two 16-bit 140 MP decodes are about four gigabytes, and on a
    machine that only had room for one that floor is the difference between
    slow and dead.  Responsiveness is not worth a crash, and since the import
    now happens entirely behind a modal there is nothing in the foreground to
    starve while it runs.
    """
    _gate.set_limit(max(1, int(n)))


# Tiers cheap enough to write whenever the file is open anyway.  "detail" is
# not one of them: it is four times the pixels of the proxy and is only wanted
# if you zoom past 40% on that particular page.
_ALWAYS = ("thumb", "proxy")


def effective_tiers(path: str, tiers, force: bool = False) -> tuple:
    """What `build` will actually write for this request.

    Callers that report progress need this: a request for a thumbnail on an
    uncached scan also produces the proxy, and a listener told only what was
    *asked for* would wait for a proxy notification that never comes.
    """
    need = [t for t in tiers if force or not os.path.exists(_cache_file(path, t))]
    if need and any(t in _ALWAYS for t in need):
        need = list(set(need) | {t for t in _ALWAYS
                                 if force or not os.path.exists(_cache_file(path, t))})
    return tuple(sorted(need, key=lambda t: -_TIERS[t]))


def build(path: str, tiers=("thumb", "proxy"), force: bool = False,
          on_progress=None) -> dict:
    """Decode `path` once and write the requested proxy tiers to the cache.

    Whenever a full decode is unavoidable, every cheap tier that is missing
    gets written, not just the ones asked for.  Decoding a 140 MP TIFF is
    seconds; shrinking the array already in memory is milliseconds.  Without
    this, a thumbnail request and a proxy request for the same scan each paid
    for their own decode - which is precisely what made importing a folder take
    twice as long as it needed to.

    Returns the probe metadata dict.
    """
    meta_path = _meta_file(path)
    need = [t for t in tiers if force or not os.path.exists(_cache_file(path, t))]

    if not need and os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass

    meta = iio.probe(path)

    if need:
        need = list(effective_tiers(path, tiers, force)) or need
        biggest = max(_TIERS[t] for t in need)
        # The decode owns most of the bar and the tier writing owns the rest,
        # so nothing happens behind a bar that is already full.
        def decode_progress(f):
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, f)) * _DECODE_SHARE)

        with _gate:
            arr = _read_reduced(path, biggest)
            if arr is not None:
                _note(path, "pyramid", "embedded reduced level")
                arr = _shrink(arr, biggest)
                if on_progress is not None:
                    on_progress(_DECODE_SHARE)
            else:
                arr = _decode(path, biggest,
                              decode_progress if on_progress else None)
            # Shrink first, convert to 8-bit after.  A 16-bit 140 MP scan
            # converted up front costs a second full-size array - 420 MB - for
            # pixels that are about to be thrown away anyway.
            order = sorted(need, key=lambda t: -_TIERS[t])
            for done, tier in enumerate(order, 1):
                small = _shrink(arr, _TIERS[tier])
                im = iio.to_pil(small)
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                # Write beside the target and rename into place.  Two threads
                # can decide to build the same file - the loader fetching the
                # page you clicked while the prefetcher warms it - and a reader
                # that catches a half-written JPEG sees a corrupt page, or in
                # the loading dialog's case calls it ready when it is not.
                dest = _cache_file(path, tier)
                tmp = f"{dest}.{os.getpid()}.{threading.get_ident()}.tmp"
                try:
                    # the format has to be named: Pillow infers it from the
                    # extension, and the temporary name ends in .tmp
                    im.save(tmp, format="JPEG", quality=94, subsampling=0,
                            optimize=False)
                    os.replace(tmp, dest)
                except BaseException:
                    # a failed write must not leave a partial file behind - the
                    # case that fails is usually a full disk, which is exactly
                    # when litter costs the most
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise
                if _TIERS[tier] >= 1000 and small is not arr:
                    arr = small       # smaller tiers shrink from this one
                if on_progress is not None:
                    on_progress(_DECODE_SHARE
                                + (1.0 - _DECODE_SHARE) * done / len(order))
            del arr

    try:
        # written the same way as the tiers, so a reader never catches half of it
        tmp = f"{meta_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        os.replace(tmp, meta_path)
    except Exception:
        pass
    return meta


def load(path: str, tier: str = "proxy", build_if_missing: bool = True) -> Optional[np.ndarray]:
    f = _cache_file(path, tier)
    if not os.path.exists(f):
        if not build_if_missing:
            return None
        build(path, tiers=(tier,))
    try:
        with Image.open(f) as im:
            im = im.convert("RGB")
            return np.asarray(im)
    except Exception:
        return None


def has(path: str, tier: str = "proxy") -> bool:
    return os.path.exists(_cache_file(path, tier))


def clear(paths=None) -> int:
    """Delete cached proxies; returns bytes freed."""
    freed = 0
    root = cache_root()
    keys = {_key(p) for p in paths} if paths else None
    for name in os.listdir(root):
        if keys is not None and name.split("_")[0].split(".")[0] not in keys:
            continue
        f = os.path.join(root, name)
        try:
            freed += os.path.getsize(f)
            os.remove(f)
        except OSError:
            pass
    return freed


def cache_size() -> int:
    root = cache_root()
    total = 0
    for name in os.listdir(root):
        try:
            total += os.path.getsize(os.path.join(root, name))
        except OSError:
            pass
    return total


# --------------------------------------------------------------------------
# background prefetch
# --------------------------------------------------------------------------

def available_memory() -> int:
    """Bytes of RAM we can reasonably use, or a conservative guess."""
    try:
        if hasattr(os, "sysconf") and "SC_AVPHYS_PAGES" in os.sysconf_names:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    try:                                                   # Windows
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MS()
        st.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullAvailPhys)
    except Exception:
        pass
    return 4 * 1024 ** 3


def suggested_workers(megapixels: float = 140.0, channels: int = 3,
                      bytes_per_sample: int = 1) -> int:
    """How many scans to decode at once without pushing the machine into swap.

    Decoding one 140 MP scan peaks around a gigabyte - the array itself plus
    the decompressor's buffers - so three at once on a 8 GB machine spends more
    time swapping than decoding.  That is what "it lags a ton while importing"
    turned out to be, and it is why this is measured rather than guessed at.
    """
    # 2.3x the finished array: measured peak for a 140 MP Deflate TIFF is about
    # 960 MB against a 420 MB result, the difference being the decompressor's
    # own buffers.
    per_page = max(120e6, megapixels * 1e6 * max(1, channels)
                   * max(1, bytes_per_sample) * 2.3)
    # Only 40% of what is free, and never more than three at once.  The figure
    # is deliberately timid: free memory shrinks under us as the file cache
    # fills with 300 MB TIFFs, and the cost of being too cautious is a slower
    # import, while the cost of being too greedy is the application dying
    # part-way through one.
    by_memory = int(available_memory() * 0.40 / per_page)
    by_cpu = max(1, (os.cpu_count() or 2) - 1)
    return max(1, min(3, by_memory, by_cpu))


class Prefetcher:
    """Keeps the next few pages in the queue warm so paging through is instant.

    One decode per file, ever.  Requests for the same scan are merged rather
    than queued separately: the queue asks for a thumbnail while the window
    asks for a thumbnail and a proxy, and treating those as two jobs meant
    every scan in an imported folder was decoded twice.
    """

    def __init__(self, workers: Optional[int] = None):
        workers = workers or suggested_workers()
        set_decode_limit(workers)
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="proxy")
        self._wanted: dict = {}            # path -> set of tiers still to build
        self._active: set = set()          # paths a worker is decoding now
        self._done: dict = {}              # path -> tiers built this session
        self._announcing: set = set()      # paths mid-notification, see request()
        self._lock = threading.Lock()
        self.on_ready: Optional[Callable[[str], None]] = None
        # Listeners get the tiers as well as the path.  A progress display that
        # cannot tell a thumbnail from a proxy marks a page finished when only
        # its thumbnail is done.
        self.listeners: list = []          # (path, tiers, ok) on finish
        self.start_listeners: list = []    # (path, tiers) when work begins
        self.progress_listeners: list = []  # (path, fraction) while decoding
        self.stopped = False

    def resize(self, workers: int) -> None:
        """Match the pool to the size of the scans actually in this project.

        The default is a guess made before any file has been looked at; once
        the headers are in we know whether these are 40 MP colour scans or
        140 MP 16-bit ones, which changes the answer by a factor of four.
        """
        workers = max(1, int(workers))
        set_decode_limit(workers)
        if workers == self._pool._max_workers or self.stopped:
            return
        old = self._pool
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="proxy")
        # cancel_futures drops work that was queued but not started, so anything
        # still wanted has to be handed to the new pool or it would simply never
        # happen - and the loading dialog would wait for it for ever
        old.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            orphaned = [p for p in self._wanted if p not in self._active]
        for path in orphaned:
            self._pool.submit(self._work, path)

    def request(self, path: str, tiers=("thumb", "proxy"), priority: bool = False) -> None:
        if self.stopped:
            # Say so rather than dropping it silently: a caller waiting on this
            # file has no other way to learn that the answer is never coming.
            self._notify(path, tuple(sorted(tiers)), False)
            return
        want = {t for t in tiers if not has(path, t)}
        submit = False
        announce = False
        with self._lock:
            # keyed by cache key, not by path: re-scanning a source file gives
            # it a new key, and "we built this already" must not be believed
            # about a file that has since changed underneath us
            want -= self._done.get(_key(path), set())
            if not want:
                # Nothing to do - but say so anyway.  Something waiting on this
                # file (the loading dialog waits on every one) would otherwise
                # wait for a notification that is never coming, because another
                # request finished the work in between.
                # Not while one is already in progress for this path: the
                # queue's thumbnail listener asks for a tier from inside the
                # notification, and if that tier is missing while we believe it
                # is built, the pair would call each other until the stack ran
                # out.
                announce = (path not in self._active
                            and path not in self._announcing)
                if announce:
                    self._announcing.add(path)
            else:
                pending = self._wanted.get(path)
                if pending is not None:
                    # a job for this file is already queued or running - add the
                    # tiers to it instead of starting a second decode
                    pending |= want
                else:
                    self._wanted[path] = set(want)
                    submit = path not in self._active
        # Never notify while holding the lock.  A listener runs arbitrary code -
        # the queue's thumbnail handler asks for another tier from inside it -
        # and re-entering request() on a non-reentrant lock deadlocks the very
        # thread that took it.
        if announce:
            try:
                self._notify(path, tuple(sorted(tiers)), True)
            finally:
                with self._lock:
                    self._announcing.discard(path)
        if submit:
            try:
                self._pool.submit(self._work, path)
            except RuntimeError:
                # the pool is shut down; the work will never happen, so report
                # it rather than leaving a waiter hanging on it
                with self._lock:
                    self._wanted.pop(path, None)
                self._notify(path, tuple(sorted(tiers)), False)

    def _work(self, path: str) -> None:
        if self.stopped:
            return
        with self._lock:
            tiers = self._wanted.pop(path, set())
            if not tiers:
                return
            self._active.add(path)

        # From here to the notification, everything is inside try/finally.  A
        # worker that dies before it reports leaves whatever is waiting on this
        # file waiting for ever - and the thing waiting is a modal dialog with
        # no close button.  It also used to leave the path in `_active`, which
        # silenced every later request for it too.
        ok = True
        again = False
        try:
            # report what will actually be written, not merely what was asked
            # for: a thumbnail request on an uncached scan produces the proxy
            # too, and a progress display waiting on the proxy has to hear it
            try:
                tiers = effective_tiers(path, tiers) or tuple(sorted(tiers))
            except Exception:
                tiers = tuple(sorted(tiers))
            for cb in self.start_listeners:
                try:
                    cb(path, tiers)
                except Exception:
                    pass
            try:
                build(path, tiers=tiers, on_progress=self._progress_for(path))
            except BaseException:
                ok = False
        finally:
            with self._lock:
                self._active.discard(path)
                if ok:
                    self._done.setdefault(_key(path), set()).update(tiers)
                # anything asked for while this was running gets its own pass
                again = bool(self._wanted.get(path))
            self._notify(path, tiers, ok)
        if again and not self.stopped:
            try:
                self._pool.submit(self._work, path)
            except RuntimeError:
                pass                      # pool is gone; nothing left to do

    def _progress_for(self, path: str):
        """A callback the decoder can report into, throttled to the eye.

        Sixty updates a page is plenty and a signal per tile is not: these
        cross a thread boundary into the interface.
        """
        if not self.progress_listeners:
            return None
        state = {"last": -1.0}

        def report(fraction: float) -> None:
            if fraction < state["last"] + 0.01 and fraction < 1.0:
                return
            state["last"] = fraction
            for cb in self.progress_listeners:
                try:
                    cb(path, fraction)
                except Exception:
                    pass
        return report

    def _notify(self, path: str, tiers: tuple, ok: bool) -> None:
        """Tell everyone this file is done with.

        Failures are reported too, not just successes: a loading dialog that
        waits for every page has to hear about the one that could not be read,
        or it waits for ever - behind a modal with no way out.
        """
        if self.on_ready is not None and ok:
            try:
                self.on_ready(path)
            except Exception:
                pass
        for cb in self.listeners:
            try:
                cb(path, tiers, ok)
            except Exception:
                pass

    def forget(self, path: str) -> None:
        """Forget that this file was built - but leave anything already queued
        for it alone, or the job would run and never report."""
        with self._lock:
            self._done.pop(_key(path), None)

    def reset(self) -> None:
        """Forget every file that was built this session.

        Needed when the cache files are deleted underneath us: without this the
        prefetcher goes on believing everything is built and quietly refuses to
        make any of it again.
        """
        with self._lock:
            self._done.clear()

    def shutdown(self) -> None:
        self.stopped = True
        self._pool.shutdown(wait=False, cancel_futures=True)
