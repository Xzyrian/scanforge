"""Full-resolution render.

The important trick here: the crop rectangle is resolved *before* the rotation
is applied, so only the pixels that survive into the final page are ever
resampled.  Rotating a whole 10000x14000 frame and then throwing 30% of it away
costs several seconds and a gigabyte of RAM per page; this costs neither.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

try:
    import cv2
except Exception:                                    # pragma: no cover
    cv2 = None

from PIL import Image

from . import imageio_util as iio
from .geometry import Rect, rotation_matrix, invert_affine, apply_affine, rotated_size
from .model import PageEdit, join_placements, join_size

RESAMPLE = {
    "Lanczos (sharpest)": "lanczos",
    "Bicubic": "cubic",
    "Bilinear (fastest)": "linear",
}

_CV_INTERP = {
    "lanczos": getattr(cv2, "INTER_LANCZOS4", 4) if cv2 else 4,
    "cubic": getattr(cv2, "INTER_CUBIC", 2) if cv2 else 2,
    "linear": getattr(cv2, "INTER_LINEAR", 1) if cv2 else 1,
}


# --------------------------------------------------------------------------
# orientation
# --------------------------------------------------------------------------

def apply_orientation(arr: np.ndarray, quadrant: int, flip_h: bool, flip_v: bool) -> np.ndarray:
    if quadrant % 4:
        arr = np.rot90(arr, k=-(quadrant % 4))       # negative -> clockwise
    if flip_h:
        arr = arr[:, ::-1]
    if flip_v:
        arr = arr[::-1, :]
    return np.ascontiguousarray(arr)


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

BLUR_MODES = ("Blur", "Pixelate")


def blur_region(arr: np.ndarray, x: int, y: int, w: int, h: int,
                radius: float, mode: str = "Blur") -> None:
    """Obscure one rectangle of `arr`, in place.

    `radius` is in pixels of `arr`, so callers working on a proxy scale it down
    and the preview matches the export.
    """
    H, W = arr.shape[:2]
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(W, int(x + w)), min(H, int(y + h))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    roi = arr[y0:y1, x0:x1]

    if mode == "Pixelate":
        block = max(2, int(round(radius)))
        sh, sw = max(1, (y1 - y0) // block), max(1, (x1 - x0) // block)
        if cv2 is not None:
            small = cv2.resize(roi, (sw, sh), interpolation=cv2.INTER_AREA)
            arr[y0:y1, x0:x1] = cv2.resize(small, (x1 - x0, y1 - y0),
                                           interpolation=cv2.INTER_NEAREST)
        else:
            ys = (np.arange(y1 - y0) * sh // max(1, y1 - y0)).clip(0, sh - 1)
            xs = (np.arange(x1 - x0) * sw // max(1, x1 - x0)).clip(0, sw - 1)
            small = roi[::max(1, (y1 - y0) // sh), ::max(1, (x1 - x0) // sw)]
            small = small[:sh, :sw]
            arr[y0:y1, x0:x1] = small[np.ix_(ys, xs)]
        return

    k = max(3, int(round(radius)) | 1)
    if cv2 is not None:
        arr[y0:y1, x0:x1] = cv2.GaussianBlur(roi, (k, k), 0,
                                             borderType=cv2.BORDER_REPLICATE)
    else:
        from PIL import ImageFilter
        im = iio.to_pil(roi).filter(ImageFilter.GaussianBlur(radius / 2.0))
        arr[y0:y1, x0:x1] = np.asarray(im).reshape(roi.shape)


def apply_blurs(arr: np.ndarray, blurs: list, origin=(0.0, 0.0),
                scale: float = 1.0) -> np.ndarray:
    """Apply a page's blur regions to an already-cropped image, in place.

    `origin` is the crop's top-left in the rotated frame and `scale` the size
    of `arr` relative to full resolution, so the same list of rectangles serves
    the 2200px preview and the 140MP export.
    """
    if not blurs:
        return arr
    ox, oy = origin
    for b in blurs:
        bx, by, bw, bh = b[0], b[1], b[2], b[3]
        radius = b[4] if len(b) > 4 else max(bw, bh) * 0.12
        mode = b[5] if len(b) > 5 else "Blur"
        blur_region(arr, (bx - ox) * scale, (by - oy) * scale,
                    bw * scale, bh * scale, max(2.0, radius * scale), mode)
    return arr


def _harmonise(pieces: list) -> list:
    """Make a set of rendered parts stackable: same channel count, same dtype.

    Two halves of a fold-out can easily come off the scanner as one greyscale
    and one colour file, and numpy will not paste one into the other.
    """
    want_colour = any(p.ndim == 3 and p.shape[2] >= 3 for p in pieces)
    want_dtype = np.uint16 if any(p.dtype == np.uint16 for p in pieces) else np.uint8
    out = []
    for p in pieces:
        if want_colour and p.ndim == 2:
            p = np.stack([p] * 3, axis=-1)
        elif want_colour and p.shape[2] == 1:
            p = np.repeat(p, 3, axis=2)
        if not want_colour and p.ndim == 3:
            p = p[:, :, 0]
        if p.dtype != want_dtype:
            p = (p.astype(np.uint16) * 257) if want_dtype == np.uint16 \
                else (p // 257).astype(np.uint8)
        out.append(p)
    return out


def build_join(edit: PageEdit, part_arrays: Optional[list] = None, *,
               interp: str = "lanczos", fill: int = 255,
               scale: float = 1.0,
               part_scales: Optional[list] = None) -> np.ndarray:
    """Assemble a joined page's parts into one image.

    `scale` is the size of the result relative to full resolution, so the same
    function builds the 2200 px preview and the finished page.  `part_arrays`
    and `part_scales` let a caller hand in proxies it already has rather than
    re-decoding every source file.
    """
    parts = edit.parts()
    places = join_placements(edit.join)
    W, H = join_size(edit.join)
    ow = max(1, int(round(W * scale)))
    oh = max(1, int(round(H * scale)))

    pieces = []
    for i, part in enumerate(parts):
        arr = part_arrays[i] if part_arrays else None
        src_scale = (part_scales[i] if part_scales else 1.0) if arr is not None else 1.0
        pieces.append(render(part, arr, interp=interp, fill=fill,
                             source_scale=src_scale,
                             output_scale=scale / src_scale))
    pieces = _harmonise(pieces)

    shape = (oh, ow) + pieces[0].shape[2:]
    canvas = np.full(shape, _fill_value(pieces[0], fill), dtype=pieces[0].dtype)
    for piece, (x, y, _w, _h) in zip(pieces, places):
        px, py = int(round(x * scale)), int(round(y * scale))
        ph, pw = piece.shape[:2]
        # rounding at two scales can disagree by a pixel; clip rather than throw
        pw = min(pw, ow - px)
        ph = min(ph, oh - py)
        if pw > 0 and ph > 0:
            canvas[py:py + ph, px:px + pw] = piece[:ph, :pw]
    return canvas


def base_preview(edit: PageEdit, tier: str = "proxy",
                 build_if_missing: bool = False,
                 full_detail: bool = False) -> Optional[np.ndarray]:
    """The page's base image at preview size - before orientation, angle, crop.

    For an ordinary page that is the cached proxy of its scan. For a joined
    page it is the parts assembled, and a part may itself be a joined page, so
    this recurses. Everything that shows a page on screen has to come through
    here: a joined page's `path` is only its *first* scan, and every place
    that read `path` directly got that one scan - the aligner treated a joined
    page as a single page, and the book view stretched the first scan across
    the width of the whole joined page.

    Returns None if any scan underneath has not been decoded yet (and
    `build_if_missing` is off), so the caller can wait rather than show half a
    page.

    A joined page is normally capped at the tier's size like any page, which
    for two scans side by side is a lower scale than either scan alone.
    `full_detail` builds it at its scans' own preview scale instead - for
    showing it next to a single scan, where the two have to match.
    """
    from . import proxycache                       # render must not need Qt
    if not edit.join:
        return proxycache.load(edit.path, tier, build_if_missing=build_if_missing)
    arrs, scales = [], []
    for part in edit.parts():
        a = base_preview(part, tier, build_if_missing, full_detail)
        if a is None:
            return None
        arrs.append(a)
        # proxies are stored unrotated, so measure against the unrotated width
        scales.append(a.shape[1] / float(part.raw_size()[0] or a.shape[1]))
    W, H = join_size(edit.join)
    cap = {"thumb": proxycache.THUMB, "detail": proxycache.DETAIL}.get(
        tier, proxycache.PROXY)
    scale = min(1.0, cap / float(max(W, H) or 1))
    if full_detail:
        # as sharp as the least sharp part - more would invent pixels
        scale = min(1.0, min(scales))
    return build_join(edit, arrs, scale=scale, part_scales=scales,
                      interp="linear")


def render(edit: PageEdit, arr: Optional[np.ndarray] = None, *,
           interp: str = "lanczos", fill: int = 255,
           output_scale: float = 1.0,
           source_scale: float = 1.0) -> np.ndarray:
    """Produce the finished page.

    `arr` may be supplied to avoid a re-read.  `source_scale` says how big `arr`
    is relative to the full-resolution source, so the very same function can
    render a proxy-sized preview for verification.
    """
    if arr is None and edit.join:
        # a page assembled from others: build the base image first, then treat
        # it exactly like any other page from here on
        arr = build_join(edit, interp=interp, fill=fill, scale=1.0)
        source_scale = 1.0
    if arr is None:
        arr = iio.read_array(edit.path)
        source_scale = 1.0

    arr = apply_orientation(arr, edit.quadrant, edit.flip_h, edit.flip_v)
    h, w = arr.shape[:2]

    angle = float(edit.angle)
    crop = edit.crop_rect()
    if crop is not None and source_scale != 1.0:
        crop = crop.scaled(source_scale)

    if abs(angle) < 1e-6:
        out = _crop_only(arr, crop, fill)
    else:
        out = _rotate_and_crop(arr, w, h, angle, crop, interp, fill)

    if edit.blurs:
        # Blur after the crop, before any downscale: the regions are stored in
        # the same frame as the crop, so this is a plain offset, and blurring
        # at full detail then resizing looks better than the other way round.
        origin = (crop.x, crop.y) if crop is not None else (0.0, 0.0)
        out = np.ascontiguousarray(out)
        apply_blurs(out, edit.blurs, origin=origin, scale=source_scale)

    if output_scale != 1.0 and output_scale > 0:
        out = _resize(out, output_scale, interp)
    return out


def _fill_value(arr: np.ndarray, fill: int) -> float:
    if arr.dtype == np.uint16:
        return float(fill) * 257.0
    return float(fill)


def _crop_only(arr: np.ndarray, crop: Optional[Rect], fill: int) -> np.ndarray:
    if crop is None:
        return arr
    r = crop.normalised().rounded()
    h, w = arr.shape[:2]
    x0, y0 = int(r.x), int(r.y)
    x1, y1 = x0 + int(r.w), y0 + int(r.h)
    if x0 >= 0 and y0 >= 0 and x1 <= w and y1 <= h:
        return np.ascontiguousarray(arr[y0:y1, x0:x1])
    # crop runs past the edge of the scan - pad rather than silently shrink
    shape = (int(r.h), int(r.w)) + arr.shape[2:]
    out = np.full(shape, _fill_value(arr, fill), dtype=arr.dtype)
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(w, x1), min(h, y1)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = arr[sy0:sy1, sx0:sx1]
    return out


def _rotate_and_crop(arr, w, h, angle, crop, interp, fill):
    M, canvas = rotation_matrix(w, h, angle)
    if crop is None:
        crop = Rect(0.0, 0.0, float(canvas[0]), float(canvas[1]))
    r = crop.normalised().rounded()
    out_w, out_h = int(r.w), int(r.h)

    # Which slice of the *source* can possibly land inside the crop?
    Minv = invert_affine(M)
    src_pts = apply_affine(Minv, r.corners())
    pad = 4
    x0 = int(max(0, math.floor(src_pts[:, 0].min()) - pad))
    y0 = int(max(0, math.floor(src_pts[:, 1].min()) - pad))
    x1 = int(min(w, math.ceil(src_pts[:, 0].max()) + pad))
    y1 = int(min(h, math.ceil(src_pts[:, 1].max()) + pad))

    if x1 <= x0 or y1 <= y0:
        shape = (out_h, out_w) + arr.shape[2:]
        return np.full(shape, _fill_value(arr, fill), dtype=arr.dtype)

    sub = np.ascontiguousarray(arr[y0:y1, x0:x1])

    # M2 = translate(-crop) . M . translate(+origin of sub)
    M2 = M.copy()
    M2[:, 2] = M[:, :2] @ np.array([x0, y0], dtype=np.float64) + M[:, 2]
    M2[0, 2] -= r.x
    M2[1, 2] -= r.y

    return _warp(sub, M2, (out_w, out_h), interp, fill)


def _warp(sub, M, size, interp, fill):
    out_w, out_h = size
    if cv2 is not None:
        flags = _CV_INTERP.get(interp, cv2.INTER_LANCZOS4)
        border = _fill_value(sub, fill)
        nch = 1 if sub.ndim == 2 else sub.shape[2]
        value = (border,) * max(1, nch)
        # cv2 warps at most 4 channels and dislikes gigantic strides; both are
        # satisfied by the sanitised arrays we hand it.
        return cv2.warpAffine(sub, M, (out_w, out_h), flags=flags,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=value)

    # Pillow fallback: it wants the inverse mapping, dst -> src
    Minv = invert_affine(M)
    im = iio.to_pil(sub)
    data = (Minv[0, 0], Minv[0, 1], Minv[0, 2], Minv[1, 0], Minv[1, 1], Minv[1, 2])
    out = im.transform((out_w, out_h), Image.AFFINE, data,
                       resample=Image.BICUBIC, fillcolor=fill)
    return np.asarray(out)


def _resize(arr: np.ndarray, scale: float, interp: str) -> np.ndarray:
    h, w = arr.shape[:2]
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    if (nw, nh) == (w, h):
        return arr
    if cv2 is not None:
        flags = cv2.INTER_AREA if scale < 1.0 else _CV_INTERP.get(interp, cv2.INTER_CUBIC)
        return cv2.resize(arr, (nw, nh), interpolation=flags)
    im = iio.to_pil(arr)
    return np.asarray(im.resize((nw, nh), Image.LANCZOS))


# --------------------------------------------------------------------------
# helpers used by the UI
# --------------------------------------------------------------------------

def preview_canvas_size(edit: PageEdit, proxy_size: tuple[int, int]) -> tuple[int, int]:
    return rotated_size(proxy_size[0], proxy_size[1], edit.angle)


def proxy_scale(edit: PageEdit, proxy_size: tuple[int, int]) -> float:
    """proxy pixels per full-resolution pixel."""
    bw, bh = edit.base_size()
    if not bw:
        return 1.0
    return proxy_size[0] / float(bw)
