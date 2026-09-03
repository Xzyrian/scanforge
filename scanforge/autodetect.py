"""One-click helpers: find the page inside the scan, and find its true skew.

Both run on the small proxy, so they answer in well under a second even for a
600dpi scan, and both only ever *propose* values - nothing is applied without
the numbers appearing in the inspector where you can nudge them.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

try:
    import cv2
except Exception:                                    # pragma: no cover
    cv2 = None

from .geometry import Rect, wrap_fine_angle


def _gray(proxy: np.ndarray) -> np.ndarray:
    if proxy.ndim == 2:
        return proxy
    if cv2 is not None:
        return cv2.cvtColor(proxy, cv2.COLOR_RGB2GRAY)
    return proxy[..., :3].mean(axis=2).astype(np.uint8)


# --------------------------------------------------------------------------
# page boundary
# --------------------------------------------------------------------------

def _inset_to_page(page: np.ndarray, x: int, y: int, w: int, h: int,
                   purity: float = 0.995, max_walk: float = 0.05) -> tuple:
    """Pull each edge of a bounding box inward until it sits fully on the sheet.

    Two things put scanner bed inside a naive bounding box: any residual skew
    leaves wedges in the corners, and the sheet's own boundary pixel is a blend
    of bed and paper.  Walking each edge in until that scanline is essentially
    all paper removes both, which is what you actually want from a "fit to page
    edges" button - nobody archives the join.
    """
    lim_x = max(1, int(w * max_walk))
    lim_y = max(1, int(h * max_walk))

    def line_ok(vals) -> bool:
        return vals.size > 0 and float(vals.mean()) >= purity

    top = y
    for _ in range(lim_y):
        if line_ok(page[top, x:x + w]):
            break
        top += 1
    bottom = y + h - 1
    for _ in range(lim_y):
        if line_ok(page[bottom, x:x + w]):
            break
        bottom -= 1
    left = x
    for _ in range(lim_x):
        if line_ok(page[top:bottom + 1, left]):
            break
        left += 1
    right = x + w - 1
    for _ in range(lim_x):
        if line_ok(page[top:bottom + 1, right]):
            break
        right -= 1

    if right - left < 8 or bottom - top < 8:
        return x, y, w, h
    return left, top, right - left + 1, bottom - top + 1


def detect_page(proxy: np.ndarray, margin_pct: float = 0.0,
                inset: bool = True) -> Optional[dict]:
    """Find the sheet against the scanner background.

    Returns {"rect": Rect (proxy px, axis-aligned), "angle": float degrees}
    or None if the page fills the frame / nothing stands out.

    `angle` is the CORRECTION to apply - feed it straight into PageEdit.angle
    and the page comes out level.
    """
    if cv2 is None:
        return None
    g = _gray(proxy)
    h, w = g.shape[:2]

    # background = the modal value around the border of the scan
    band = 6
    border = np.concatenate([
        g[:band].ravel(), g[-band:].ravel(),
        g[:, :band].ravel(), g[:, -band:].ravel(),
    ])
    bg = int(np.median(border))
    spread = float(np.std(border)) + 6.0

    diff = cv2.absdiff(g, np.full_like(g, bg))
    thr = max(14, min(60, int(spread * 3)))
    mask = (diff > thr).astype(np.uint8) * 255

    k = max(3, (min(w, h) // 200) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n < 2:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    area = stats[idx, cv2.CC_STAT_AREA]
    if area < 0.04 * w * h:
        return None
    if area > 0.985 * w * h:
        return None                                   # page already fills the scan

    comp = (labels == idx).astype(np.uint8)
    cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)

    (cx, cy), (rw, rh), ang = cv2.minAreaRect(cnt)
    # cv2 reports the angle in [-90, 0); fold it to a small skew
    angle = wrap_fine_angle(-ang)
    if abs(angle) > 8.0:
        angle = 0.0

    x, y, bw_, bh_ = cv2.boundingRect(cnt)
    if inset:
        filled = np.zeros(comp.shape, np.uint8)
        cv2.drawContours(filled, [cnt], -1, 1, thickness=cv2.FILLED)
        # Shed the ring of boundary pixels that are a blend of bed and paper.
        # Eroding the silhouette does this without any reference to brightness,
        # so a scanline full of dark text still counts as page.
        filled = cv2.erode(filled, np.ones((3, 3), np.uint8), iterations=2)
        x, y, bw_, bh_ = _inset_to_page(filled > 0, x, y, bw_, bh_)
    if margin_pct:
        dx, dy = bw_ * margin_pct / 100.0, bh_ * margin_pct / 100.0
        x, y, bw_, bh_ = x + dx, y + dy, bw_ - 2 * dx, bh_ - 2 * dy

    return {"rect": Rect(float(x), float(y), float(bw_), float(bh_)),
            "angle": float(angle),
            "quad": np.int32(cv2.boxPoints(((cx, cy), (rw, rh), ang)))}


def detect_page_rotated(proxy: np.ndarray, angle: float,
                        margin_pct: float = 0.0) -> Optional[Rect]:
    """Detect the page bounds *after* a straightening angle has been applied.

    This is what the 'fit crop to page' button uses, so the returned rectangle
    is directly usable as a crop in the rotated frame.
    """
    if cv2 is None:
        return None
    from .geometry import rotation_matrix
    h, w = proxy.shape[:2]
    if abs(angle) > 1e-6:
        M, canvas = rotation_matrix(w, h, angle)
        # replicate, so the corners the rotation exposes look like more scanner
        # bed rather than a second high-contrast object competing with the sheet
        proxy = cv2.warpAffine(proxy, M, canvas, flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)
    found = detect_page(proxy, margin_pct)
    return found["rect"] if found else None


# --------------------------------------------------------------------------
# skew from text lines
# --------------------------------------------------------------------------

def detect_skew(proxy: np.ndarray, limit: float = 3.0, coarse: float = 0.1,
                fine: float = 0.01) -> float:
    """Projection-profile deskew: the angle whose horizontal ink profile has the
    sharpest peaks is the angle at which the text lines are level.

    Returns the CORRECTION angle - assign it to PageEdit.angle (or add it to
    the current one) and the page comes out level.  So a page whose text runs
    downhill to the right, i.e. rotated clockwise, yields a negative number.

    Accurate to ~0.01 degrees, which is the resolution this workflow needs.
    """
    if cv2 is None:
        return 0.0
    g = _gray(proxy)
    h, w = g.shape[:2]
    # 1600px is where sub-0.05-degree skews stop being lost in the resampling;
    # below that a 0.05 degree tilt is under a pixel of rise across the page
    scale = 1600.0 / max(w, h)
    if scale < 1.0:
        g = cv2.resize(g, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    g = cv2.GaussianBlur(g, (3, 3), 0)
    binv = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 12)
    # trim the outer 6% so the sheet edge itself does not dominate the profile
    hh, ww = binv.shape
    m = int(min(hh, ww) * 0.06)
    if hh - 2 * m > 40 and ww - 2 * m > 40:
        binv = binv[m:hh - m, m:ww - m]
    if binv.mean() < 1.0:
        return 0.0

    best = _profile_search(binv, np.arange(-limit, limit + coarse, coarse))
    lo, hi = best - coarse, best + coarse
    best = _profile_search(binv, np.arange(lo, hi + fine, fine))
    return float(round(best, 3))


def _profile_search(binv: np.ndarray, angles: np.ndarray) -> float:
    h, w = binv.shape
    center = (w / 2.0, h / 2.0)
    best_score, best_angle = -1.0, 0.0
    for a in angles:
        if abs(a) < 1e-9:
            rot = binv
        else:
            M = cv2.getRotationMatrix2D(center, float(-a), 1.0)   # cv2: +ve = CCW
            rot = cv2.warpAffine(binv, M, (w, h), flags=cv2.INTER_NEAREST,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        prof = rot.sum(axis=1, dtype=np.float64)
        score = float(np.diff(prof).var())
        if score > best_score:
            best_score, best_angle = score, float(a)
    return best_angle


# --------------------------------------------------------------------------
# content bounds, for aligning facing pages
# --------------------------------------------------------------------------

def content_bounds(proxy: np.ndarray, ink_pct: float = 0.4) -> Optional[Rect]:
    """Bounding box of actual printed content, ignoring paper and edge grime."""
    if cv2 is None:
        return None
    g = _gray(proxy)
    g = cv2.GaussianBlur(g, (5, 5), 0)
    binv = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, 41, 14)
    h, w = binv.shape
    cols = binv.sum(axis=0) / 255.0
    rows = binv.sum(axis=1) / 255.0
    cth, rth = h * ink_pct / 100.0, w * ink_pct / 100.0
    xs = np.where(cols > cth)[0]
    ys = np.where(rows > rth)[0]
    if xs.size < 2 or ys.size < 2:
        return None
    return Rect(float(xs[0]), float(ys[0]),
                float(xs[-1] - xs[0] + 1), float(ys[-1] - ys[0] + 1))
