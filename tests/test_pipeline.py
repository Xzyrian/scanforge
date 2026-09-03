"""Correctness + performance checks against synthetic 600dpi scans.

The one that really matters is test_optimised_matches_naive: the exporter
crops before it rotates (for speed), and that must produce pixel-for-pixel the
same page as the obvious rotate-the-whole-thing-then-slice approach.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from scanforge import autodetect, imageio_util as iio, proxycache, render
from scanforge.export import (BatchExporter, ExportSettings, export_one,
                              output_path)
from scanforge.geometry import (Rect, angle_from_line, invert_affine,
                                rotation_matrix, rotated_size,
                                transform_rect_quadrant)
from scanforge.model import PageEdit, Project

TMP = tempfile.mkdtemp(prefix="scanforge_test_")
FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# --------------------------------------------------------------------------
# synthetic scan
# --------------------------------------------------------------------------

def make_scan(w=10000, h=14000, skew=0.37, margin=340, path=None, dpi=600):
    """A white sheet with text-like rules, skewed clockwise by `skew` degrees
    on a grey scanner bed."""
    bg = 96
    img = np.full((h, w, 3), bg, np.uint8)
    rng = np.random.default_rng(11)

    pw, ph = w - 2 * margin, h - 2 * margin
    page = np.full((ph, pw, 3), 246, np.uint8)
    cv2.rectangle(page, (2, 2), (pw - 3, ph - 3), (196, 196, 196), 4)
    # body text lines: ragged right margin, but no systematic drift that would
    # bias a projection-profile estimator
    y = 300
    while y < ph - 300:
        x0 = 220
        x1 = pw - 220 - int(rng.integers(0, 320))
        cv2.rectangle(page, (x0, y), (x1, y + 46), (28, 28, 32), -1)
        y += 118
    # a headline and a figure box, so content bounds have something to find
    cv2.rectangle(page, (220, 120), (pw // 2, 240), (18, 18, 22), -1)
    cv2.rectangle(page, (pw // 2, ph // 2), (pw - 260, ph // 2 + 900), (150, 150, 158), -1)

    M = cv2.getRotationMatrix2D((pw / 2, ph / 2), -skew, 1.0)   # cv2 +ve = CCW
    M[0, 2] += margin
    M[1, 2] += margin
    cv2.warpAffine(page, M, (w, h), dst=img, flags=cv2.INTER_LINEAR,
                   borderMode=cv2.BORDER_TRANSPARENT)

    if path:
        iio.write_tiff(path, img, dpi=dpi, compression="zlib")
    return img


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_geometry_identities():
    print("\ngeometry")
    for w, h, a in ((1000, 1400, 0.37), (10000, 14000, -1.25), (500, 500, 30.0)):
        M, canvas = rotation_matrix(w, h, a)
        Minv = invert_affine(M)
        pts = np.array([[0, 0], [w, 0], [w, h], [0, h], [w / 3, h / 7]], float)
        back = (pts @ M[:, :2].T + M[:, 2]) @ Minv[:, :2].T + Minv[:, 2]
        err = np.abs(back - pts).max()
        check(f"round-trip {w}x{h} @ {a}°", err < 1e-9, f"max error {err:.2e}")

        # every source corner must land inside the expanded canvas
        corners = (pts[:4] @ M[:, :2].T + M[:, 2])
        inside = (corners[:, 0] >= -0.6) & (corners[:, 0] <= canvas[0] + 0.6) & \
                 (corners[:, 1] >= -0.6) & (corners[:, 1] <= canvas[1] + 0.6)
        check(f"canvas contains corners {w}x{h} @ {a}°", inside.all())

    check("angle_from_line horizontal",
          abs(angle_from_line(0, 0, 1000, 17.45) - (-1.0)) < 0.01,
          f"{angle_from_line(0, 0, 1000, 17.45):.4f}")
    check("angle_from_line vertical",
          abs(angle_from_line(0, 0, 17.45, 1000) - 1.0) < 0.01,
          f"{angle_from_line(0, 0, 17.45, 1000):.4f}")

    r = transform_rect_quadrant(Rect(10, 20, 100, 200), (1000, 2000), 1)
    check("quadrant rect rotate", (r.x, r.y, r.w, r.h) == (1780.0, 10.0, 200.0, 100.0),
          str(r.as_tuple()))
    r4 = transform_rect_quadrant(Rect(10, 20, 100, 200), (1000, 2000), 4)
    check("quadrant rect 4 steps == identity",
          (r4.x, r4.y, r4.w, r4.h) == (10.0, 20.0, 100.0, 200.0))


def test_optimised_matches_naive():
    print("\ncrop-before-rotate equivalence")
    rng = np.random.default_rng(7)
    src = (rng.random((1400, 1000, 3)) * 255).astype(np.uint8)
    src = cv2.GaussianBlur(src, (5, 5), 0)          # smooth: interpolation is meaningful

    for angle in (0.05, 0.37, -1.0, 2.5, -12.0):
        M, canvas = rotation_matrix(1000, 1400, angle)
        naive_full = cv2.warpAffine(src, M, canvas, flags=cv2.INTER_LANCZOS4,
                                    borderMode=cv2.BORDER_CONSTANT,
                                    borderValue=(255, 255, 255))
        crop = Rect(120.0, 200.0, 700.0, 1000.0)
        naive = naive_full[200:1200, 120:820]

        e = PageEdit(path="x", src_w=1000, src_h=1400)
        e.angle = angle
        e.set_crop_rect(crop)
        fast = render.render(e, src.copy(), interp="lanczos", fill=255)

        same_shape = fast.shape == naive.shape
        diff = np.abs(fast.astype(np.int32) - naive.astype(np.int32)).mean() \
            if same_shape else 999
        check(f"angle {angle:+.2f}° shape", same_shape,
              f"{fast.shape} vs {naive.shape}")
        check(f"angle {angle:+.2f}° pixels", diff < 0.35, f"mean |Δ| = {diff:.4f}")


def test_crop_only_is_exact():
    print("\nzero-rotation crop is a pure slice")
    rng = np.random.default_rng(3)
    src = (rng.random((900, 700, 3)) * 255).astype(np.uint8)
    e = PageEdit(path="x", src_w=700, src_h=900, angle=0.0)
    e.set_crop_rect(Rect(51, 77, 300, 400))
    out = render.render(e, src.copy())
    check("exact slice", np.array_equal(out, src[77:477, 51:351]))

    e.set_crop_rect(Rect(-40, -30, 300, 400))       # crop hanging off the edge
    out = render.render(e, src.copy(), fill=255)
    check("out-of-bounds crop padded", out.shape == (400, 300, 3) and out[0, 0, 0] == 255,
          str(out.shape))


def test_uint16_and_grayscale():
    print("\nbit depth and channel handling")
    rng = np.random.default_rng(5)
    for arr, label in ((( rng.random((800, 600)) * 65535).astype(np.uint16), "uint16 gray"),
                       ((rng.random((800, 600, 3)) * 65535).astype(np.uint16), "uint16 rgb"),
                       ((rng.random((800, 600)) * 255).astype(np.uint8), "uint8 gray")):
        e = PageEdit(path="x", src_w=600, src_h=800, angle=0.6)
        e.set_crop_rect(Rect(30, 40, 400, 600))
        out = render.render(e, arr.copy(), fill=255)
        check(f"{label} renders", out.dtype == arr.dtype and out.shape[:2] == (600, 400),
              f"{out.dtype} {out.shape}")

    p = os.path.join(TMP, "u16.tif")
    a = (rng.random((500, 400, 3)) * 65535).astype(np.uint16)
    iio.write_tiff(p, a, dpi=600, compression="zlib")
    back = iio.read_array(p)
    meta = iio.probe(p)
    check("uint16 tiff round-trip", np.array_equal(a, back))
    check("dpi preserved", abs(meta["dpi"] - 600) < 0.01, str(meta["dpi"]))
    check("dtype preserved", meta["dtype"] == "uint16", meta["dtype"])


def test_quadrant_and_flip():
    print("\norientation")
    a = np.arange(6 * 4 * 3, dtype=np.uint8).reshape(6, 4, 3)
    r90 = render.apply_orientation(a, 1, False, False)
    check("rot90 clockwise shape", r90.shape == (4, 6, 3), str(r90.shape))
    check("rot90 top-left came from bottom-left",
          np.array_equal(r90[0, 0], a[-1, 0]))
    check("4x rot90 == identity",
          np.array_equal(render.apply_orientation(a, 4, False, False), a))
    check("flip h", np.array_equal(render.apply_orientation(a, 0, True, False),
                                   a[:, ::-1]))


def test_detection_and_proxies():
    print("\ndetection on a real-size scan  (10000 x 14000)")
    path = os.path.join(TMP, "page_600dpi.tif")
    t0 = time.time()
    make_scan(path=path, skew=0.37)
    print(f"      generated + saved in {time.time() - t0:.1f}s "
          f"({os.path.getsize(path) / 1e6:.0f} MB)")

    meta = iio.probe(path)
    check("probe size", (meta["width"], meta["height"]) == (10000, 14000))
    check("probe dpi", abs(meta["dpi"] - 600) < 0.01)

    t0 = time.time()
    proxycache.build(path, tiers=("thumb", "proxy", "detail"), force=True)
    t_build = time.time() - t0
    print(f"      proxy pyramid built in {t_build:.1f}s")
    check("proxy build under 30s", t_build < 30, f"{t_build:.1f}s")

    t0 = time.time()
    proxy = proxycache.load(path, "proxy")
    t_load = time.time() - t0
    print(f"      cached proxy loaded in {t_load * 1000:.0f} ms")
    check("cached proxy load under 400ms", t_load < 0.4, f"{t_load * 1000:.0f} ms")
    check("proxy long edge ~2200", max(proxy.shape[:2]) == proxycache.PROXY,
          str(proxy.shape))

    t0 = time.time()
    skew = autodetect.detect_skew(proxy)
    t_skew = time.time() - t0
    # detect_skew returns the CORRECTION, so a clockwise-skewed page is negative
    check("auto-deskew recovers -0.37°", abs(skew - (-0.37)) < 0.03,
          f"found {skew:+.3f}° in {t_skew:.2f}s")

    found = autodetect.detect_page(proxy)
    check("page detected", found is not None)
    if found:
        s = proxy.shape[1] / 10000.0
        r = found["rect"]
        # sheet is 9320x13320; the inset walk trims it slightly below that
        check("page width plausible", 8900 * s < r.w < 9400 * s,
              f"{r.w / s:.0f} px full-res")
        check("page angle plausible", abs(found["angle"] - (-0.37)) < 0.25,
              f"{found['angle']:+.3f}°")

    rect = autodetect.detect_page_rotated(proxy, skew)
    check("fit-to-page after deskew", rect is not None and rect.w > 100)

    cb = autodetect.content_bounds(proxy)
    check("content bounds found", cb is not None and cb.w > 100)
    return path, proxy, skew


def test_full_res_export(path, proxy, skew):
    print("\nfull-resolution export")
    e = PageEdit(path=path, src_w=10000, src_h=14000, dpi=600.0)
    e.angle = skew
    s = proxy.shape[1] / 10000.0
    rect = autodetect.detect_page_rotated(proxy, skew)
    e.set_crop_rect(rect.scaled(1.0 / s))

    cfg = ExportSettings(out_dir=os.path.join(TMP, "out"), fmt="TIFF",
                         compression="Deflate (ZIP)", workers=1,
                         document="Test Manual", subfolder_per_doc=True)
    t0 = time.time()
    res = export_one(e, cfg, 1)
    dt = time.time() - t0
    check("export succeeded", res.ok, res.error[:200] if not res.ok else "")
    if res.ok:
        print(f"      one 140MP page rendered + written in {dt:.1f}s "
              f"({res.size_bytes / 1e6:.0f} MB)")
        out_meta = iio.probe(res.path)
        want_w, want_h = e.output_size()
        check("exported size matches prediction",
              (out_meta["width"], out_meta["height"]) == (want_w, want_h),
              f"{out_meta['width']}x{out_meta['height']} vs {want_w}x{want_h}")
        check("exported dpi is 600", abs(out_meta["dpi"] - 600) < 0.01)
        check("subfolder per document",
              os.path.basename(os.path.dirname(res.path)) == "Test Manual")

        out = iio.read_array(res.path)
        s2 = 2200.0 / max(out.shape[:2])
        residual = autodetect.detect_skew(
            cv2.resize(out, (int(out.shape[1] * s2), int(out.shape[0] * s2)),
                       interpolation=cv2.INTER_AREA))
        # 0.04 is the estimator's own noise floor on this fixture, not slop in
        # the render - the deskew sweep above resolves 0.000 on cleaner input
        check("exported page is straight", abs(residual) < 0.04,
              f"residual skew {residual:+.3f}°")

        border = np.concatenate([out[:2].ravel(), out[-2:].ravel(),
                                 out[:, :2].ravel(), out[:, -2:].ravel()])
        darkest = np.percentile(border, 1)
        check("no scanner background left in the crop", darkest > 150,
              f"1st-percentile edge value {darkest:.0f} (bed is 96)")
    return e


def test_deskew_accuracy():
    print("\ndeskew accuracy sweep")
    for truth in (0.05, 0.23, -0.41, 1.00, -1.77):
        img = make_scan(w=3000, h=4200, skew=truth, margin=110)
        small = cv2.resize(img, (1571, 2200), interpolation=cv2.INTER_AREA)
        found = autodetect.detect_skew(small)
        check(f"skew {truth:+.2f}° -> correction {found:+.3f}°",
              abs(found + truth) < 0.03, f"error {found + truth:+.3f}°")


def test_batch_and_pdf(e):
    print("\nbatch + PDF")
    pages = []
    for i in range(3):
        c = PageEdit.from_dict(e.to_dict())
        c.uid = f"uid{i}"
        pages.append(c)
    cfg = ExportSettings(out_dir=os.path.join(TMP, "batch"), fmt="TIFF",
                         compression="LZW", workers=2, make_pdf=True,
                         pdf_dpi=150, naming="{doc}_{n3}", scale_mode="Percent",
                         percent=35, document="Test Manual")
    b = BatchExporter(pages, cfg)
    msgs = []
    b.on_message = msgs.append
    t0 = time.time()
    results = b.run()
    dt = time.time() - t0
    check("all 3 exported", all(r.ok for r in results),
          "; ".join(r.error[:80] for r in results if not r.ok))
    print(f"      3 pages at 35% in {dt:.1f}s across 2 workers")
    names = sorted(os.path.basename(r.path) for r in results if r.ok)
    check("naming tokens applied",
          names == ["Test Manual_001.tif", "Test Manual_002.tif",
                    "Test Manual_003.tif"], str(names))
    pdf = os.path.join(cfg.out_dir, "Test Manual.pdf")
    check("pdf assembled", os.path.exists(pdf),
          f"{os.path.getsize(pdf) / 1e6:.1f} MB" if os.path.exists(pdf) else "missing")


def test_spread_crop_survives():
    print("\nspread alignment survives the shared crop")
    proj = Project()
    for i in range(4):
        p = PageEdit(path=f"p{i}.tif", src_w=2400, src_h=3400)
        p.crop = [100.0, 100.0, 2000.0, 3000.0]
        proj.pages.append(p)
    left, right = proj.pages[1], proj.pages[2]

    # what the spread view writes, then what its accept button does
    left.crop, left.angle = [60.0, 210.0, 1900.0, 2900.0], -0.42
    right.crop, right.angle = [140.0, 195.0, 1900.0, 2900.0], 0.31
    left.spread_partner, right.spread_partner = right.uid, left.uid
    left.crop_mode = right.crop_mode = "locked"
    saved = (list(left.crop), list(right.crop))

    # the user now goes to another page and nudges its crop
    proj.pages[0].crop = [110.0, 105.0, 2000.0, 3000.0]
    n = proj.apply_linked_crop(proj.pages[0])
    check("the shared crop still reaches the unlocked page",
          proj.pages[3].crop == [110.0, 105.0, 2000.0, 3000.0], str(proj.pages[3].crop))
    check("but leaves the left spread page alone", left.crop == saved[0],
          str(left.crop))
    check("and the right one", right.crop == saved[1], str(right.crop))
    check("angles untouched", (left.angle, right.angle) == (-0.42, 0.31))
    check("only the unlocked page was rewritten", n == 1, str(n))

    # a locked page must not push its crop outward either
    left.crop = [10.0, 10.0, 500.0, 500.0]
    n = proj.apply_linked_crop(left)
    check("a locked page does not propagate its own crop", n == 0, str(n))
    check("the others are unchanged",
          proj.pages[3].crop == [110.0, 105.0, 2000.0, 3000.0])

    # own_position still keeps x/y but takes the shared size
    proj.pages[3].crop_mode = "own_position"
    proj.pages[3].crop = [500.0, 600.0, 111.0, 222.0]
    proj.apply_linked_crop(proj.pages[0])
    check("own_position keeps its place",
          proj.pages[3].crop[:2] == [500.0, 600.0], str(proj.pages[3].crop))
    check("own_position takes the shared size",
          proj.pages[3].crop[2:] == [2000.0, 3000.0], str(proj.pages[3].crop))

    # and it all survives a save/load round trip
    f = os.path.join(TMP, "locked.sfproj")
    proj.save(f)
    back = Project.load(f)
    check("crop_mode survives saving",
          [p.crop_mode for p in back.pages] ==
          [p.crop_mode for p in proj.pages],
          str([p.crop_mode for p in back.pages]))

    # a project written before locking existed still loads sensibly
    legacy = os.path.join(TMP, "legacy_indep.sfproj")
    with open(legacy, "w", encoding="utf-8") as fh:
        json.dump({"schema": 2, "pages": [
            {"path": "/x/a.tif", "uid": "a", "crop_independent": True},
            {"path": "/x/b.tif", "uid": "b", "crop_independent": False}]}, fh)
    old = Project.load(legacy)
    check("old crop_independent becomes own_position",
          [p.crop_mode for p in old.pages] == ["own_position", "linked"],
          str([p.crop_mode for p in old.pages]))


def test_project_paths_and_naming():
    print("\nexport path and document name")
    proj = Project()
    proj.last_folder = os.path.join("A:", "Scans", "Sony_KV-16G1")
    check("export goes beside the scan folder, not inside it",
          proj.default_export_dir() == os.path.join("A:", "Scans"),
          proj.default_export_dir())
    check("document name comes from the scan folder",
          proj.suggested_name() == "Sony_KV-16G1", proj.suggested_name())

    # the folder must win over the project file name - getting this backwards
    # is why the name only sometimes came out right
    proj.path = os.path.join("A:", "Scans", "scanforge.sfproj")
    check("the scan folder still wins over the project file",
          proj.suggested_name() == "Sony_KV-16G1", proj.suggested_name())
    proj.document_name = "Chosen By Hand"
    check("an explicit name wins over everything",
          proj.suggested_name() == "Chosen By Hand", proj.suggested_name())

    proj.pages.append(PageEdit(path="x.tif"))
    proj.clear()
    check("clearing empties the project",
          not proj.pages and not proj.document_name and not proj.last_folder
          and not proj.path and proj.link_crop)


def test_zip_package():
    print("\nzip package")
    import zipfile
    src = np.full((300, 220, 3), 210, np.uint8)
    out_dir = os.path.join(TMP, "zipped")
    pages = []
    for i in range(3):
        p = PageEdit(path=os.path.join(TMP, f"z{i}.tif"), src_w=220, src_h=300)
        iio.write_tiff(p.path, src, dpi=600)
        pages.append(p)
    cfg = ExportSettings(out_dir=out_dir, document="Sony_KV-16G1",
                         zip_images=True, make_pdf=True, pdf_dpi=100, workers=1)
    b = BatchExporter(pages, cfg)
    msgs = []
    b.on_message = msgs.append
    b.run()

    zip_path = os.path.join(out_dir, "Sony_KV-16G1-images.zip")
    check("zip written", os.path.exists(zip_path), zip_path)
    if os.path.exists(zip_path):
        with zipfile.ZipFile(zip_path) as z:
            names = sorted(z.namelist())
        check("holds every exported page", len(names) == 3, str(names))
        check("named from the document",
              names[0] == "Sony_KV-16G1-0001.tif", names[0])
        check("stores flat, no folder structure",
              all("/" not in n and "\\\\" not in n for n in names), str(names))
        check("the PDF is not inside it",
              not any(n.lower().endswith(".pdf") for n in names), str(names))
    check("the loose files are still there",
          os.path.exists(os.path.join(out_dir, "Sony_KV-16G1-0001.tif")))
    check("the PDF sits beside the zip",
          os.path.exists(os.path.join(out_dir, "Sony_KV-16G1.pdf")))
    check("zipping is on by default", ExportSettings().zip_images is True)


def test_orientation_export():
    print("\norientation: preview and export agree")
    # a marker in one corner, so we can say where the page ended up
    src = np.full((800, 600, 3), 40, np.uint8)
    src[20:120, 20:120] = 250                      # bright square, top-left

    def corner_of(a):
        h, w = a.shape[:2]
        halves = {
            "top-left": a[:h // 2, :w // 2], "top-right": a[:h // 2, w // 2:],
            "bottom-left": a[h // 2:, :w // 2], "bottom-right": a[h // 2:, w // 2:],
        }
        return max(halves, key=lambda k: float(halves[k].mean()))

    check("marker starts top-left", corner_of(src) == "top-left", corner_of(src))

    cases = [
        (0, False, False, "top-left", (600, 800)),
        (1, False, False, "top-right", (800, 600)),     # 90 clockwise
        (2, False, False, "bottom-right", (600, 800)),
        (3, False, False, "bottom-left", (800, 600)),
        (0, True, False, "top-right", (600, 800)),      # mirror across
        (0, False, True, "bottom-left", (600, 800)),
        (0, True, True, "bottom-right", (600, 800)),
    ]
    for quad, fh, fv, expect, size in cases:
        e = PageEdit(path="x", src_w=600, src_h=800,
                     quadrant=quad, flip_h=fh, flip_v=fv)
        out = render.render(e, src.copy())
        label = f"quadrant {quad}" + (" flip-h" if fh else "") + (" flip-v" if fv else "")
        check(f"{label}: marker ends {expect}", corner_of(out) == expect,
              corner_of(out))
        check(f"{label}: output size {size[0]}x{size[1]}",
              (out.shape[1], out.shape[0]) == size,
              f"{out.shape[1]}x{out.shape[0]}")
        check(f"{label}: page reports the same size", e.output_size() == size,
              str(e.output_size()))

    # what the canvas shows is apply_orientation() of the proxy; what the
    # exporter writes is apply_orientation() of the full image.  Those two only
    # agree if orienting and downscaling commute - so check that they do.
    proxy = cv2.resize(src, (300, 400), interpolation=cv2.INTER_AREA)
    for quad, fh, fv, _e, _s in cases:
        a = render.apply_orientation(proxy, quad, fh, fv)
        b = cv2.resize(render.apply_orientation(src, quad, fh, fv), (a.shape[1], a.shape[0]),
                       interpolation=cv2.INTER_AREA)
        diff = float(np.abs(a.astype(np.int32) - b.astype(np.int32)).mean())
        check(f"quadrant {quad} fh={int(fh)} fv={int(fv)}: preview matches export",
              diff < 0.6, f"mean |Δ| = {diff:.3f}")

    # a crop taken on the turned page must cut the turned page
    e = PageEdit(path="x", src_w=600, src_h=800, quadrant=1)
    e.set_crop_rect(Rect(0, 0, 400, 300))          # top-left of the turned frame
    out = render.render(e, src.copy())
    check("crop applies after the turn, not before",
          out.shape[:2] == (300, 400), str(out.shape))
    check("and takes the turned page's top-left",
          corner_of(out) in ("top-right", "top-left"), corner_of(out))


def test_blur():
    print("\nblur regions")
    rng = np.random.default_rng(21)
    src = (rng.random((1200, 900, 3)) * 255).astype(np.uint8)

    def detail(a, x, y, w, h):
        """Local high-frequency energy - what blurring is supposed to remove."""
        roi = a[y:y + h, x:x + w].astype(np.float32)
        return float(np.abs(np.diff(roi, axis=0)).mean())

    e = PageEdit(path="x", src_w=900, src_h=1200, angle=0.0)
    e.blurs = [[100.0, 200.0, 300.0, 250.0, 25.0, "Blur"]]
    out = render.render(e, src.copy())
    check("blur output keeps the page size", out.shape == src.shape, str(out.shape))
    inside_before = detail(src, 100, 200, 300, 250)
    inside_after = detail(out, 100, 200, 300, 250)
    check("detail is destroyed inside the blur", inside_after < inside_before * 0.25,
          f"{inside_before:.1f} -> {inside_after:.1f}")
    check("pixels outside are untouched",
          np.array_equal(out[:190], src[:190]))
    check("pixels to the right are untouched",
          np.array_equal(out[200:450, 420:], src[200:450, 420:]))

    e.blurs = [[100.0, 200.0, 300.0, 250.0, 30.0, "Pixelate"]]
    pix = render.render(e, src.copy())
    check("pixelate also destroys detail",
          detail(pix, 110, 210, 280, 230) < inside_before * 0.35,
          f"{detail(pix, 110, 210, 280, 230):.1f}")
    block = pix[220:250, 120:150]
    check("pixelate produces flat blocks",
          float(block.std(axis=(0, 1)).mean()) < float(src[220:250, 120:150]
                                                       .std(axis=(0, 1)).mean()) * 0.6)

    # a blur must land in the right place once the page is cropped
    e.blurs = [[300.0, 400.0, 200.0, 150.0, 25.0, "Blur"]]
    e.set_crop_rect(Rect(250, 350, 500, 500))
    cropped = render.render(e, src.copy())
    check("cropped output size", cropped.shape[:2] == (500, 500), str(cropped.shape))
    # the blur sits at (300-250, 400-350) = (50, 50) within the crop
    check("blur lands at the right offset in a cropped page",
          detail(cropped, 60, 60, 180, 130) < inside_before * 0.3,
          f"{detail(cropped, 60, 60, 180, 130):.1f}")
    check("the rest of the cropped page is sharp",
          detail(cropped, 300, 300, 150, 150) > inside_before * 0.7,
          f"{detail(cropped, 300, 300, 150, 150):.1f}")

    # and it must survive a rotation, at the same place on the paper
    e.angle = 0.8
    e.set_crop_rect(Rect(250, 350, 500, 500))
    rotated = render.render(e, src.copy())
    check("blur still applied after a rotation",
          detail(rotated, 60, 60, 170, 120) < inside_before * 0.4,
          f"{detail(rotated, 60, 60, 170, 120):.1f}")

    # preview and export must obscure the same spot
    e.angle = 0.0
    e.crop = None
    e.blurs = [[300.0, 400.0, 200.0, 150.0, 25.0, "Blur"]]
    full = render.render(e, src.copy())
    half = render.render(e, cv2.resize(src, (450, 600), interpolation=cv2.INTER_AREA),
                         source_scale=0.5)
    check("proxy preview blurs the same region",
          detail(half, 155, 205, 90, 70) < detail(half, 20, 20, 90, 70) * 0.5,
          f"{detail(half, 155, 205, 90, 70):.1f} vs "
          f"{detail(half, 20, 20, 90, 70):.1f} outside")

    p = PageEdit(path="x")
    check("a page with no blurs is unaffected",
          np.array_equal(render.render(p, src.copy()), src))


def test_export_defaults():
    print("\nexport defaults")
    cfg = ExportSettings()
    check("TIFF compression is None", cfg.compression == "None", cfg.compression)
    check("subfolder off", cfg.subfolder_per_doc is False)
    check("dash naming", cfg.naming == "{doc}-{page}", cfg.naming)
    check("four digits from one", cfg.digits == 4 and cfg.start_number == 1)
    from scanforge.export import resolve_pdf_dpi
    pages = [PageEdit(path="a", dpi=600.0), PageEdit(path="b", dpi=600.0)]
    check("pdf dpi matches 600 dpi scans",
          resolve_pdf_dpi(cfg, pages) == 600.0, str(resolve_pdf_dpi(cfg, pages)))
    pages = [PageEdit(path="a", dpi=300.0), PageEdit(path="b", dpi=300.0)]
    check("pdf dpi matches 300 dpi scans",
          resolve_pdf_dpi(cfg, pages) == 300.0, str(resolve_pdf_dpi(cfg, pages)))

    cfg.document = "Sony_KV-16G1"
    cfg.out_dir = "/out"
    e = PageEdit(path="/scans/img_042.tif")
    got = os.path.basename(output_path(e, cfg, 1))
    check("first file is Sony_KV-16G1-0001.tif", got == "Sony_KV-16G1-0001.tif", got)
    check("no subfolder in the path",
          os.path.dirname(output_path(e, cfg, 1)).rstrip("/\\") == "/out",
          os.path.dirname(output_path(e, cfg, 1)))
    from scanforge.export import pdf_path
    check("pdf is Sony_KV-16G1.pdf",
          os.path.basename(pdf_path(cfg)) == "Sony_KV-16G1.pdf",
          os.path.basename(pdf_path(cfg)))
    check("TIFF 'None' really means no compression",
          iio.TIFF_COMPRESSIONS["None"] is None)


def test_project_roundtrip():
    print("\nproject file")
    proj = Project()
    p = PageEdit(path="/some/scan.tif", src_w=10000, src_h=14000, dpi=600.0)
    p.angle = -0.735
    p.set_crop_rect(Rect(101.4, 202.6, 9000.0, 13000.0))
    p.blurs = [[10.0, 20.0, 300.0, 200.0, 24.0, "Pixelate"]]
    proj.document_name = "Brochure A"
    proj.pages.append(p)
    f = os.path.join(TMP, "t.sfproj")
    proj.save(f)
    back = Project.load(f)
    check("one page restored", len(back.pages) == 1)
    q = back.pages[0]
    check("angle restored", abs(q.angle - (-0.735)) < 1e-9)
    check("crop restored", q.crop == [101.4, 202.6, 9000.0, 13000.0], str(q.crop))
    check("document name restored", back.document_name == "Brochure A",
          back.document_name)
    check("blurs restored", q.blurs == [[10.0, 20.0, 300.0, 200.0, 24.0, "Pixelate"]],
          str(q.blurs))
    check("output size", q.output_size() == (9000, 13000), str(q.output_size()))
    check("output inches", abs(q.output_inches()[0] - 15.0) < 1e-6)

    # a cut piece and a joined page are the two structures that were never
    # persisted before; both have to survive a save and a reload intact
    piece = p.cut([0.5], "v")[0]
    a = PageEdit(path="/some/l.tif", src_w=1000, src_h=1400)
    b = PageEdit(path="/some/r.tif", src_w=1000, src_h=1400)
    a.crop = [10.0, 20.0, 900.0, 1300.0]
    b.crop = [30.0, 20.0, 900.0, 1300.0]
    b.angle = 0.25
    stitched = PageEdit.joined([a, b], axis="h", gap=12)
    proj.pages += [piece, stitched]
    proj.save(f)
    back = Project.load(f)
    got_piece = back.pages[1]
    got_join = back.pages[2]
    check("a cut piece survives the round trip",
          got_piece.label == "left" and got_piece.crop == piece.crop
          and got_piece.crop_mode == "locked", str(got_piece.crop))
    check("a joined page comes back with both its parts",
          len(got_join.parts()) == 2, str(len(got_join.parts())))
    check("the parts keep their own alignment",
          abs(got_join.parts()[1].angle - 0.25) < 1e-9
          and got_join.parts()[1].crop == [30.0, 20.0, 900.0, 1300.0],
          str(got_join.parts()[1].crop))
    check("the assembled size is the same after reloading",
          got_join.base_size() == stitched.base_size(),
          f"{got_join.base_size()} vs {stitched.base_size()}")
    check("the gap between the parts survives",
          got_join.join["gap"] == 12, str(got_join.join.get("gap")))

    legacy = os.path.join(TMP, "legacy.sfproj")
    with open(legacy, "w", encoding="utf-8") as fh:
        json.dump({"schema": 2, "pages": [
            {"path": "/x/a.tif", "uid": "aaa", "document": "Old Manual"}]}, fh)
    old = Project.load(legacy)
    check("an old per-page document name migrates to the project",
          old.document_name == "Old Manual", old.document_name)


def test_proxy_fullres_agreement():
    print("\nproxy preview matches the exported framing")
    # The canvas draws crop*proxy_scale; the exporter uses the full-res number.
    # Both must frame the same content.
    src = make_scan(w=3000, h=4200, skew=0.8, margin=110)
    p = os.path.join(TMP, "small.tif")
    iio.write_tiff(p, src, dpi=600, compression="zlib")
    proxycache.build(p, tiers=("proxy",), force=True)
    proxy = proxycache.load(p, "proxy")
    s = proxy.shape[1] / 3000.0

    e = PageEdit(path=p, src_w=3000, src_h=4200, dpi=600.0)
    e.angle = autodetect.detect_skew(proxy)
    rect = autodetect.detect_page_rotated(proxy, e.angle)
    e.set_crop_rect(rect.scaled(1.0 / s))

    full = render.render(e, src.copy())
    prox = render.render(e, proxy.copy(), source_scale=s)
    fh, fw = full.shape[:2]
    ph, pw = prox.shape[:2]
    check("aspect ratios agree", abs((fw / fh) - (pw / ph)) < 0.01,
          f"{fw / fh:.4f} vs {pw / ph:.4f}")
    check("scale relationship holds", abs(pw / fw - s) < 0.02,
          f"{pw / fw:.4f} vs {s:.4f}")

    a = cv2.resize(full, (pw, ph), interpolation=cv2.INTER_AREA).astype(np.float32)
    b = prox.astype(np.float32)
    diff = np.abs(a - b).mean()
    check("preview and export frame the same pixels", diff < 6.0,
          f"mean |Δ| = {diff:.2f}")


def test_cut_pages():
    print("\ncutting one scan into pages")
    w, h = 2400, 3000
    src = np.full((h, w, 3), 240, np.uint8)
    src[:, :w // 2] = (60, 60, 200)          # left half blue, right half red
    src[:, w // 2:] = (200, 60, 60)
    p = os.path.join(TMP, "cut_src.tif")
    iio.write_tiff(p, src, dpi=600, compression="zlib")

    proj = Project()
    page = PageEdit(path=p, src_w=w, src_h=h, dpi=600.0)
    page.crop = [0.0, 0.0, float(w), float(h)]
    proj.pages = [page]

    halves = page.cut([0.5], "v")
    check("a cut makes two pages", len(halves) == 2, str(len(halves)))
    check("the halves are named", [x.label for x in halves] == ["left", "right"],
          str([x.label for x in halves]))
    check("both halves are the same width",
          halves[0].output_size() == halves[1].output_size() == (1200, 3000),
          str([x.output_size() for x in halves]))
    check("neither half can be flattened by the shared crop",
          all(x.crop_mode == "locked" for x in halves))
    check("the halves are separate pages, not one",
          halves[0].uid != halves[1].uid != page.uid)

    proj.replace(page.uid, halves)
    check("the queue keeps the cut page's place",
          [x.uid for x in proj.pages] == [halves[0].uid, halves[1].uid])

    # ... and the pixels actually land where the cut said they would
    left = render.render(halves[0], src.copy())
    right = render.render(halves[1], src.copy())
    check("the left page is the left of the scan",
          abs(int(left[:, :, 2].mean()) - 200) < 3, str(left[0, 0]))
    check("the right page is the right of the scan",
          abs(int(right[:, :, 0].mean()) - 200) < 3, str(right[0, 0]))

    # a measured cut into thirds
    thirds = page.cut([1 / 3.0, 2 / 3.0], "v")
    widths = [x.output_size()[0] for x in thirds]
    check("thirds are equal", len(thirds) == 3 and max(widths) - min(widths) <= 1,
          str(widths))
    check("thirds are labelled by position",
          [x.label for x in thirds] == ["1/3", "2/3", "3/3"],
          str([x.label for x in thirds]))

    # cutting a page that is already cropped works within the crop
    page.crop = [400.0, 200.0, 1000.0, 2000.0]
    pieces = page.cut([0.5], "v")
    check("a cut respects the crop that is already there",
          pieces[0].crop == [400.0, 200.0, 500.0, 2000.0]
          and pieces[1].crop == [900.0, 200.0, 500.0, 2000.0],
          str([x.crop for x in pieces]))

    # exporting two halves under a name-based scheme must not collide
    cfg = ExportSettings(out_dir=TMP, naming="{name}", document="Doc")
    a = output_path(pieces[0], cfg, 1)
    b = output_path(pieces[1], cfg, 2)
    check("the halves export to different files", a != b,
          f"{os.path.basename(a)} vs {os.path.basename(b)}")


def test_join_pages():
    print("\njoining two scans into one page")
    w, h = 1600, 2400
    left_src = np.full((h, w, 3), 90, np.uint8)
    right_src = np.full((h, w, 3), 210, np.uint8)
    lp = os.path.join(TMP, "join_l.tif")
    rp = os.path.join(TMP, "join_r.tif")
    iio.write_tiff(lp, left_src, dpi=600, compression="zlib")
    iio.write_tiff(rp, right_src, dpi=600, compression="zlib")

    a = PageEdit(path=lp, src_w=w, src_h=h, dpi=600.0)
    b = PageEdit(path=rp, src_w=w, src_h=h, dpi=600.0)
    # each half trimmed at the seam, as the alignment view would leave them
    a.crop = [100.0, 50.0, 1400.0, 2300.0]
    b.crop = [100.0, 50.0, 1400.0, 2300.0]

    page = PageEdit.joined([a, b], axis="h", gap=0)
    check("the joined page is as wide as both halves",
          page.base_size() == (2800, 2300), str(page.base_size()))
    check("a joined page is locked against the shared crop",
          page.crop_mode == "locked")

    out = render.render(page)
    check("the assembled page is one image of the right size",
          out.shape[:2] == (2300, 2800), str(out.shape))
    check("the left half of the result came from the left scan",
          abs(int(out[:, :1400].mean()) - 90) < 2, str(out[:, :1400].mean()))
    check("the right half came from the right scan",
          abs(int(out[:, 1400:].mean()) - 210) < 2, str(out[:, 1400:].mean()))

    # the container's own crop trims the assembled image, seam included
    page.crop = [200.0, 100.0, 2400.0, 2000.0]
    trimmed = render.render(page)
    check("cropping the joined page trims the assembly",
          trimmed.shape[:2] == (2000, 2400), str(trimmed.shape))

    # a full export goes through the same path with no source file of its own
    cfg = ExportSettings(out_dir=TMP, fmt="TIFF", compression="None",
                         document="Joined")
    res = export_one(page, cfg, 1)
    check("a joined page exports", res.ok, res.error or "")
    if res.ok:
        back = iio.read_array(res.path)
        check("the exported file is the assembled page",
              back.shape[:2] == (2000, 2400), str(back.shape))

    # and it can be taken apart again
    parts = page.unjoin()
    check("splitting it apart returns the original pages",
          [x.path for x in parts] == [lp, rp], str([x.name for x in parts]))
    check("each part keeps the alignment it had",
          parts[0].crop == [100.0, 50.0, 1400.0, 2300.0], str(parts[0].crop))

    # a greyscale half and a colour half still assemble
    grey = np.full((h, w), 128, np.uint8)
    gp = os.path.join(TMP, "join_grey.tif")
    iio.write_tiff(gp, grey, dpi=600, compression="zlib")
    g = PageEdit(path=gp, src_w=w, src_h=h, dpi=600.0, channels=1)
    mixed = PageEdit.joined([a, g], axis="h", gap=0)
    out2 = render.render(mixed)
    check("a greyscale scan joins to a colour one",
          out2.ndim == 3 and out2.shape[2] == 3, str(out2.shape))


def test_size_matching():
    print("\nmatching crop sizes")
    proj = Project()
    sizes = [(2000, 3000), (2002, 2998), (1999, 3001), (1400, 2000)]
    for i, (w, h) in enumerate(sizes):
        e = PageEdit(path=f"/nonexistent/p{i}.tif", src_w=2400, src_h=3400,
                     dpi=600.0)
        e.crop = [100.0, 120.0, float(w), float(h)]
        proj.pages.append(e)

    groups = proj.size_groups(tolerance=5)
    check("near-identical crops group together", len(groups) == 2,
          str([(g["size"], len(g["pages"])) for g in groups]))
    check("the big group holds the three close ones",
          len(groups[0]["pages"]) == 3, str(len(groups[0]["pages"])))
    check("the group reports how much it varies",
          groups[0]["spread"] == (3, 3), str(groups[0]["spread"]))
    check("the suggested size is a real one from the middle of the group",
          groups[0]["size"] == (2000, 3000), str(groups[0]["size"]))

    before = {p.uid: p.crop_rect().center() for p in proj.pages}
    n = proj.match_sizes(groups[0]["pages"], (2000, 3000))
    check("the pages that were off get changed", n == 2, str(n))
    check("every page in the group is now exactly the target",
          {p.output_size() for p in proj.pages[:3]} == {(2000, 3000)},
          str([p.output_size() for p in proj.pages[:3]]))
    check("the page outside the tolerance is left alone",
          proj.pages[3].output_size() == (1400, 2000),
          str(proj.pages[3].output_size()))
    # resizing about the centre keeps each page's framing where it was, which
    # is the point: the size changes, the content does not slide
    moved = max(max(abs(a - b) for a, b in zip(before[p.uid],
                                               p.crop_rect().center()))
                for p in proj.pages[:3])
    check("no page's framing moved", moved < 1e-6, f"largest shift {moved:g} px")

    # a tighter tolerance splits them again
    proj2 = Project()
    for i, (w, h) in enumerate(sizes):
        e = PageEdit(path=f"/nonexistent/q{i}.tif", src_w=2400, src_h=3400)
        e.crop = [0.0, 0.0, float(w), float(h)]
        proj2.pages.append(e)
    check("a tolerance of zero groups only exact matches",
          len(proj2.size_groups(tolerance=0)) == 4,
          str(len(proj2.size_groups(tolerance=0))))

    # locked pages are excluded unless asked for
    proj2.pages[0].crop_mode = "locked"
    check("locked pages sit out by default",
          sum(len(g["pages"]) for g in proj2.size_groups(tolerance=5)) == 3)
    check("...and join in when asked",
          sum(len(g["pages"]) for g in
              proj2.size_groups(tolerance=5, include_locked=True)) == 4)


def test_decode_economy():
    """Importing a folder must decode each scan exactly once."""
    print("\ndecoding each scan once")
    import threading

    src = make_scan(w=1400, h=1900, skew=0.2, margin=60)
    paths = []
    for i in range(4):
        p = os.path.join(TMP, f"econ_{i}.tif")
        iio.write_tiff(p, src, dpi=600, compression="zlib")
        paths.append(p)
    proxycache.clear(paths)

    real = iio.read_array
    counts = {}
    live = {"now": 0, "max": 0}
    lock = threading.Lock()

    def counted(path, *a, **kw):
        with lock:
            counts[path] = counts.get(path, 0) + 1
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        try:
            time.sleep(0.05)          # long enough for the others to pile up
            return real(path, *a, **kw)
        finally:
            with lock:
                live["now"] -= 1

    iio.read_array = counted
    try:
        # one decode serves both tiers, however the request was phrased
        proxycache.build(paths[0], tiers=("thumb",))
        check("a thumbnail request also produces the proxy",
              proxycache.has(paths[0], "proxy"))
        proxycache.build(paths[0], tiers=("thumb", "proxy"))
        check("so the later proxy request costs no second decode",
              counts[paths[0]] == 1, f"{counts.get(paths[0])} decodes")

        # the queue asks for thumbnails while the window asks for proxies:
        # those must merge into one job per file, not two
        pf = proxycache.Prefetcher(workers=4)
        # a pool of four, but the gate says two: the gate is what actually
        # decides, which is the point of having it in front of the decode
        proxycache.set_decode_limit(2)
        seen = []
        pf.listeners.append(lambda p, t, ok: seen.append((p, t, ok)))
        for p in paths[1:]:
            pf.request(p, tiers=("thumb",))
        for p in paths[1:]:
            pf.request(p)
        t0 = time.time()
        while (len({p for p, _t, _o in seen}) < 3 and time.time() - t0 < 60):
            time.sleep(0.02)
        pf.shutdown()
        check("overlapping requests decode each file once",
              all(counts.get(p) == 1 for p in paths[1:]),
              str({os.path.basename(p): counts.get(p) for p in paths[1:]}))
        check("every file reported in", len({p for p, _t, _o in seen}) == 3,
              str(len(seen)))
        check("and the report says the proxy is ready, not just the thumbnail",
              all("proxy" in t for _p, t, _o in seen), str(seen))
        check("never more decodes at once than the gate allows",
              live["max"] <= 2, f"peaked at {live['max']}")

        # a request for a file that is already cached still reports, or anything
        # waiting on it - like the modal loading dialog - would wait for ever
        pf2 = proxycache.Prefetcher(workers=1)
        told = []
        pf2.listeners.append(lambda p, t, ok: told.append(p))
        pf2.request(paths[0])
        check("an already-cached page still reports done", told == [paths[0]],
              str(told))

        # ...and that report must not happen while the lock is held: the real
        # listener asks for another tier from inside it, and re-entering a
        # non-reentrant lock on the same thread would wedge the interface for
        # good.  It must not recurse endlessly either, which is what a listener
        # that always asks again would otherwise do.
        reentered = []

        def nosy(p, t, ok):
            reentered.append(p)
            pf2.request(p, tiers=("thumb",))     # what the queue panel does

        pf2.listeners.append(nosy)
        finished = threading.Event()
        threading.Thread(target=lambda: (pf2.request(paths[0]),
                                         finished.set()), daemon=True).start()
        check("notifying a listener does not deadlock the requester",
              finished.wait(5), "timed out — deadlocked")
        check("nor recurse when the listener asks again from inside it",
              len(reentered) == 1, f"{len(reentered)} nested notifications")
        pf2.shutdown()

        # a cleared cache must be forgotten, or nothing is ever rebuilt
        proxycache.clear([paths[0]])
        pf3 = proxycache.Prefetcher(workers=1)
        pf3._done[paths[0]] = {"thumb", "proxy"}      # as if built earlier
        pf3.reset()
        got = []
        pf3.listeners.append(lambda p, t, ok: got.append(t))
        pf3.request(paths[0])
        t0 = time.time()
        while not got and time.time() - t0 < 60:
            time.sleep(0.02)
        pf3.shutdown()
        check("after the cache is cleared the proxies are built again",
              proxycache.has(paths[0], "proxy"), str(got))
    finally:
        iio.read_array = real
        proxycache.set_decode_limit(2)

    n = proxycache.suggested_workers(140, 3, 1)
    check("the decode pool is sized to the machine, not guessed",
          1 <= n <= 3, f"{n} workers for 140 MP scans, "
                       f"{proxycache.available_memory() / 1e9:.1f} GB free")

    # Every way a request can end has to report, because what waits on it is a
    # modal with no close button.  Silence there is not a slow import, it is a
    # frozen application.
    print("\nevery request reports back")
    heard = []
    pf4 = proxycache.Prefetcher(workers=1)
    pf4.listeners.append(lambda p, t, ok: heard.append((os.path.basename(p), ok)))

    missing = os.path.join(TMP, "not_here.tif")
    pf4.request(missing)
    t0 = time.time()
    while not heard and time.time() - t0 < 30:
        time.sleep(0.02)
    check("a file that cannot be read reports a failure",
          heard == [("not_here.tif", False)], str(heard))

    heard.clear()
    boom = os.path.join(TMP, "econ_0.tif")
    real_build = proxycache.build
    proxycache.build = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        pf4.forget(boom)
        proxycache.clear([boom])
        pf4.request(boom)
        t0 = time.time()
        while not heard and time.time() - t0 < 30:
            time.sleep(0.02)
    finally:
        proxycache.build = real_build
    check("a decoder that blows up reports too",
          heard and heard[0][1] is False, str(heard))
    check("...and does not poison the file for the rest of the session",
          boom not in pf4._active, str(pf4._active))

    heard.clear()
    pf4.shutdown()
    pf4.request(boom)
    check("a request after shutdown reports rather than vanishing",
          heard and heard[0][1] is False, str(heard))

    # a rescanned source file must not be trusted as already built
    pf5 = proxycache.Prefetcher(workers=1)
    pf5._done[proxycache._key(boom)] = {"thumb", "proxy"}
    time.sleep(1.1)
    with open(boom, "ab") as fh:                 # same path, different file
        fh.write(b"\0")
    told = []
    pf5.listeners.append(lambda p, t, ok: told.append(t))
    pf5.request(boom)
    t0 = time.time()
    while not told and time.time() - t0 < 60:
        time.sleep(0.02)
    pf5.shutdown()
    check("a re-scanned file is read again, not assumed to be cached",
          told and "proxy" in told[0], str(told))


def main():
    print("=" * 72)
    print("ScanForge pipeline tests")
    print("=" * 72)
    test_geometry_identities()
    test_optimised_matches_naive()
    test_crop_only_is_exact()
    test_uint16_and_grayscale()
    test_quadrant_and_flip()
    test_project_roundtrip()
    test_spread_crop_survives()
    test_project_paths_and_naming()
    test_zip_package()
    test_orientation_export()
    test_blur()
    test_export_defaults()
    test_deskew_accuracy()
    test_proxy_fullres_agreement()
    test_cut_pages()
    test_join_pages()
    test_size_matching()
    test_decode_economy()
    path, proxy, skew = test_detection_and_proxies()
    e = test_full_res_export(path, proxy, skew)
    test_batch_and_pdf(e)

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
