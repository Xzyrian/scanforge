"""Geometry: the single source of truth for how a page is straightened and cropped.

Coordinate spaces
-----------------
source frame   pixels of the image on disk, after the 90-degree "orientation"
               step (quadrant rotation + flips) has been applied.
rotated frame  the source frame rotated by `angle` degrees about its centre,
               on an expanded canvas so nothing is clipped.  The crop
               rectangle lives here, stored in FULL-RESOLUTION pixels.

Angle convention: positive `angle` rotates the image CLOCKWISE on screen.
This matches Qt's QTransform.rotate() so the interactive preview and the
final full-resolution render agree exactly, with no sign juggling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------

def rotated_size(w: float, h: float, angle: float) -> tuple[int, int]:
    """Size of the expanded canvas that holds `w`x`h` rotated by `angle`."""
    r = math.radians(angle)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    return max(1, int(round(w * c + h * s))), max(1, int(round(w * s + h * c)))


def rotation_matrix(w: float, h: float, angle: float) -> tuple[np.ndarray, tuple[int, int]]:
    """Affine mapping source frame -> rotated frame.

    Returns (M, (W, H)) where M is 2x3 and dst = M @ [x, y, 1].
    """
    W, H = rotated_size(w, h, angle)
    r = math.radians(angle)
    c, s = math.cos(r), math.sin(r)
    cx, cy = w / 2.0, h / 2.0
    # clockwise on a y-down raster
    m = np.array([[c, -s], [s, c]], dtype=np.float64)
    tx = W / 2.0 - (m[0, 0] * cx + m[0, 1] * cy)
    ty = H / 2.0 - (m[1, 0] * cx + m[1, 1] * cy)
    M = np.array([[m[0, 0], m[0, 1], tx],
                  [m[1, 0], m[1, 1], ty]], dtype=np.float64)
    return M, (W, H)


def invert_affine(M: np.ndarray) -> np.ndarray:
    """Invert a 2x3 affine matrix."""
    A = np.vstack([M, [0.0, 0.0, 1.0]])
    return np.linalg.inv(A)[:2, :]


def apply_affine(M: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply a 2x3 affine to an (N, 2) array of points."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    return pts @ M[:, :2].T + M[:, 2]


# --------------------------------------------------------------------------
# rectangles
# --------------------------------------------------------------------------

@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)

    def scaled(self, s: float) -> "Rect":
        return Rect(self.x * s, self.y * s, self.w * s, self.h * s)

    def corners(self) -> np.ndarray:
        return np.array([
            [self.x, self.y],
            [self.x + self.w, self.y],
            [self.x + self.w, self.y + self.h],
            [self.x, self.y + self.h],
        ], dtype=np.float64)

    def normalised(self) -> "Rect":
        x, y, w, h = self.x, self.y, self.w, self.h
        if w < 0:
            x, w = x + w, -w
        if h < 0:
            y, h = y + h, -h
        return Rect(x, y, w, h)

    def rounded(self) -> "Rect":
        """Snap to whole pixels, preserving the requested width and height.

        Growing the box to cover a fractional origin (floor/ceil) would hand
        back a page one pixel wider than the number the user typed, which then
        disagrees with the size shown in the inspector.  Width wins.
        """
        return Rect(math.floor(self.x + 0.5), math.floor(self.y + 0.5),
                    max(1, int(round(self.w))), max(1, int(round(self.h))))

    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)


def full_rect(size: tuple[int, int]) -> Rect:
    return Rect(0.0, 0.0, float(size[0]), float(size[1]))


def output_dims(rect: Rect) -> tuple[int, int]:
    """Pixel dimensions the renderer will produce for `rect`.

    The inspector must predict exactly what the exporter writes, so both go
    through this one function rather than rounding independently.
    """
    r = rect.normalised().rounded()
    return int(r.w), int(r.h)


# --------------------------------------------------------------------------
# quadrant (90-degree) orientation, used to keep a crop valid when the user
# rotates a page by 90 degrees after already drawing one.
# --------------------------------------------------------------------------

def quadrant_size(w: int, h: int, quadrant: int) -> tuple[int, int]:
    return (h, w) if quadrant % 2 else (w, h)


def transform_rect_quadrant(rect: Rect, canvas: tuple[int, int], steps: int) -> Rect:
    """Rotate `rect` by `steps` * 90 degrees clockwise inside `canvas`."""
    x, y, w, h = rect.as_tuple()
    W, H = canvas
    for _ in range(steps % 4):
        x, y, w, h = (H - (y + h), x, h, w)
        W, H = H, W
    return Rect(x, y, w, h)


# --------------------------------------------------------------------------
# straighten-from-a-drawn-line
# --------------------------------------------------------------------------

def angle_from_line(x1: float, y1: float, x2: float, y2: float,
                    axis: str = "auto") -> float:
    """Angle (degrees, clockwise-positive) that makes the drawn line axis-aligned.

    `axis` is "horizontal", "vertical" or "auto" (picks whichever the line is
    already closest to).
    """
    dx, dy = x2 - x1, y2 - y1
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0.0
    if axis == "auto":
        axis = "horizontal" if abs(dx) >= abs(dy) else "vertical"
    if axis == "horizontal":
        a = math.degrees(math.atan2(dy, dx))
        if a > 90:
            a -= 180
        elif a < -90:
            a += 180
        return -a
    a = math.degrees(math.atan2(dx, dy))
    if a > 90:
        a -= 180
    elif a < -90:
        a += 180
    return a


def wrap_fine_angle(angle: float) -> float:
    """Fold an angle into (-45, 45]; the whole-quadrant part is handled separately."""
    while angle > 45.0:
        angle -= 90.0
    while angle <= -45.0:
        angle += 90.0
    return angle
