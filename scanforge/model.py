"""Non-destructive edit model.

Nothing is ever written back over a source scan.  Every adjustment lives in a
`PageEdit`, and the whole session is stored as one JSON project file so you can
close the app mid-brochure and pick it up later.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

from .geometry import Rect

SCHEMA_VERSION = 3


def join_size(join: dict) -> tuple[int, int]:
    """Pixel size of the assembled image described by `join`."""
    sizes = [PageEdit.from_dict(d).output_size() for d in join.get("parts", [])]
    if not sizes:
        return (1, 1)
    gap = max(0, int(join.get("gap", 0)))
    if join.get("axis", "h") == "h":
        return (sum(w for w, _ in sizes) + gap * (len(sizes) - 1),
                max(h for _, h in sizes))
    return (max(w for w, _ in sizes),
            sum(h for _, h in sizes) + gap * (len(sizes) - 1))


def join_placements(join: dict) -> list:
    """Where each part lands in the assembled image: [(x, y, w, h), ...].

    Parts are butted together along the axis and centred across it, so two
    halves of slightly different height meet on their middles rather than
    hanging off one edge.
    """
    parts = [PageEdit.from_dict(d) for d in join.get("parts", [])]
    sizes = [p.output_size() for p in parts]
    gap = max(0, int(join.get("gap", 0)))
    W, H = join_size(join)
    out, cur = [], 0
    for w, h in sizes:
        if join.get("axis", "h") == "h":
            out.append((cur, (H - h) // 2, w, h))
            cur += w + gap
        else:
            out.append(((W - w) // 2, cur, w, h))
            cur += h + gap
    return out


@dataclass
class PageEdit:
    """Everything the app knows about one scanned page."""

    path: str
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # --- source facts, filled in once the image has been probed -------------
    src_w: int = 0
    src_h: int = 0
    dpi: float = 600.0
    channels: int = 3
    dtype: str = "uint8"

    # --- orientation (applied first, lossless) -----------------------------
    quadrant: int = 0          # 0/1/2/3 -> 0/90/180/270 clockwise
    flip_h: bool = False
    flip_v: bool = False

    # --- fine straightening -------------------------------------------------
    angle: float = 0.0         # degrees, clockwise-positive, typically |a| < 3

    # --- crop, in FULL-RES pixels of the rotated frame ----------------------
    crop: Optional[list] = None      # [x, y, w, h]; None means "whole page"

    # --- obscured regions ---------------------------------------------------
    # [x, y, w, h, radius, mode] in the rotated frame at full resolution, i.e.
    # the same space as `crop`, so a blur placed over a spot stays over it.
    blurs: list = field(default_factory=list)

    # --- grouping -----------------------------------------------------------
    spread_partner: str = ""   # uid of the facing page, if aligned as a spread
    spread_side: str = ""      # "left" or "right"

    # --- pages made out of other pages ---------------------------------------
    # A *join* is one page assembled from several, e.g. a fold-out photographed
    # in two overlapping passes.  `join["parts"]` holds a complete PageEdit dict
    # per piece - each with its own orientation, angle and crop, which is what
    # aligning the pieces actually produces - and the container's own `crop`
    # then applies to the assembled image.  Nothing is written to disk until
    # export; the pieces remain untouched source files.
    join: Optional[dict] = None
    # {"parts": [pagedict, ...], "axis": "h"|"v", "gap": int}

    # A short suffix shown after the file name in the queue, so the two halves
    # of a cut scan are distinguishable when they share a source file.
    label: str = ""

    # --- bookkeeping --------------------------------------------------------
    done: bool = False
    note: str = ""

    # What "crop all pages" does to this page:
    #   "linked"       - takes the whole rectangle (the default)
    #   "own_position" - keeps its own x/y, takes the shared width and height,
    #                    for a sheet that sat differently on the glass
    #   "locked"       - never touched by propagation, in either direction.
    #                    Spread-aligned pages are locked automatically, because
    #                    their framing was set against their facing page and
    #                    overwriting it silently destroys careful work.
    crop_mode: str = "linked"

    # ---------------------------------------------------------------- helpers
    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def display_name(self) -> str:
        """What the queue shows.  Two halves of a cut scan share a file name,
        so the label is what tells them apart."""
        return f"{self.name} · {self.label}" if self.label else self.name

    def raw_size(self) -> tuple[int, int]:
        """The page's image before any 90-degree turn: what a proxy of it is
        stored as. For a joined page that is the assembled image."""
        if self.join:
            return join_size(self.join)
        return self.src_w, self.src_h

    def base_size(self) -> tuple[int, int]:
        """Size of the frame the angle and crop are measured against.

        For a joined page that is the assembled image, not any one source file.
        """
        w, h = self.raw_size()
        if self.quadrant % 2:
            return h, w
        return w, h

    # ------------------------------------------------------------------ cutting
    def frame_rect(self) -> Rect:
        """The area this page currently occupies in the rotated frame."""
        if self.crop:
            return Rect(*self.crop).normalised()
        from .geometry import rotated_size
        w, h = self.base_size()
        W, H = rotated_size(w, h, self.angle)
        return Rect(0.0, 0.0, float(W), float(H))

    def cut(self, fractions: list, axis: str = "v") -> list["PageEdit"]:
        """Split this page into pieces without touching the file.

        `axis` "v" means a vertical cut line, i.e. the page divides left/right.
        `fractions` are positions along that axis within the current crop, as
        values in (0, 1).  Each piece is a new page over the same source with a
        narrower crop, and is locked so a crop-all leaves it alone -
        the whole point of a cut page is that it is *not* the same shape as the
        rest of the document.
        """
        import copy as _copy

        r = self.frame_rect()
        cuts = sorted(f for f in fractions if 0.0 < f < 1.0)
        if not cuts:
            return [self]
        span = r.w if axis == "v" else r.h
        edges = [0.0] + [f * span for f in cuts] + [span]

        n = len(edges) - 1
        if n == 2:
            names = ("left", "right") if axis == "v" else ("top", "bottom")
        else:
            names = tuple(f"{i + 1}/{n}" for i in range(n))

        out = []
        for i in range(n):
            piece = _copy.deepcopy(self)
            piece.uid = uuid.uuid4().hex[:12]
            piece.spread_partner = ""
            piece.spread_side = ""
            piece.crop_mode = "locked"
            piece.done = False
            piece.label = " ".join(x for x in (self.label, names[i]) if x)
            a, b = edges[i], edges[i + 1]
            if axis == "v":
                piece.crop = [r.x + a, r.y, b - a, r.h]
            else:
                piece.crop = [r.x, r.y + a, r.w, b - a]
            # a blur belongs to the piece its centre falls in
            px, py, pw, ph = piece.crop
            piece.blurs = [b2 for b2 in piece.blurs
                           if px <= b2[0] + b2[2] / 2.0 <= px + pw
                           and py <= b2[1] + b2[3] / 2.0 <= py + ph]
            out.append(piece)
        return out

    def crop_rect(self) -> Optional[Rect]:
        if not self.crop:
            return None
        return Rect(*self.crop)

    def set_crop_rect(self, rect: Optional[Rect]) -> None:
        self.crop = None if rect is None else list(rect.normalised().as_tuple())

    def is_edited(self) -> bool:
        return bool(self.crop) or abs(self.angle) > 1e-6 or self.quadrant \
            or self.flip_h or self.flip_v or bool(self.blurs) or bool(self.join)

    def output_size(self) -> tuple[int, int]:
        from .geometry import output_dims, rotated_size
        w, h = self.base_size()
        if self.crop:
            return output_dims(Rect(*self.crop))
        return rotated_size(w, h, self.angle)

    def output_inches(self) -> tuple[float, float]:
        w, h = self.output_size()
        d = self.dpi or 600.0
        return w / d, h / d

    def copy_adjustments_from(self, other: "PageEdit") -> None:
        """Propagate framing to another page scanned in the same position."""
        self.quadrant = other.quadrant
        self.flip_h = other.flip_h
        self.flip_v = other.flip_v
        self.angle = other.angle
        self.crop = list(other.crop) if other.crop else None

    # ------------------------------------------------------------------ joining
    @classmethod
    def joined(cls, parts: list, axis: str = "h", gap: int = 0) -> "PageEdit":
        """Assemble several aligned pages into one page.

        Each part keeps the orientation, angle and crop it was given while
        being aligned - that alignment *is* the join - and the new page's own
        crop then trims the assembled image, which is how the seam edges get
        cut off.
        """
        parts = [p for p in parts if p is not None]
        if not parts:
            raise ValueError("a joined page needs at least one part")
        join = {"parts": [p.to_dict() for p in parts], "axis": axis,
                "gap": max(0, int(gap))}
        w, h = join_size(join)
        first = parts[0]
        page = cls(path=first.path, src_w=w, src_h=h, dpi=first.dpi,
                   channels=max(p.channels for p in parts),
                   dtype=first.dtype, join=join)
        # A joined page is not the shape of the rest of the document, so the
        # a crop-all must leave it alone.
        page.crop_mode = "locked"
        page.label = "joined"
        return page

    def parts(self) -> list:
        return [PageEdit.from_dict(d) for d in (self.join or {}).get("parts", [])]

    def source_paths(self) -> list:
        """Every scan this page is drawn from, however deeply it was joined.

        `path` alone is not enough for a joined page: it is only the first
        scan, and a join of a join has scans that no single level lists.
        """
        if not self.join:
            return [self.path]
        out = []
        for part in self.parts():
            for p in part.source_paths():
                if p not in out:
                    out.append(p)
        return out

    def unjoin(self) -> list:
        """Take a joined page back apart into the pages it was made from."""
        return self.parts() or [self]

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def crop_locked(self) -> bool:
        return self.crop_mode == "locked"

    @classmethod
    def from_dict(cls, d: dict) -> "PageEdit":
        known = {f for f in cls.__dataclass_fields__}
        page = cls(**{k: v for k, v in d.items() if k in known})
        if "crop_mode" not in d and d.get("crop_independent"):
            page.crop_mode = "own_position"      # projects from before the lock
        return page


@dataclass
class Project:
    pages: list = field(default_factory=list)
    path: str = ""
    export_dir: str = ""
    last_folder: str = ""
    # a scanned document is one object photographed many times; by default
    # every page therefore gets the same crop, and so the same output size
    # one name for the whole set - these are pages of a single brochure or
    # manual, not a folder of unrelated documents.  Set at export time.
    document_name: str = ""

    # ---------------------------------------------------------------- lookup
    def by_uid(self, uid: str) -> Optional[PageEdit]:
        for p in self.pages:
            if p.uid == uid:
                return p
        return None

    def index_of(self, uid: str) -> int:
        for i, p in enumerate(self.pages):
            if p.uid == uid:
                return i
        return -1

    def paths(self) -> set:
        return {os.path.normcase(os.path.abspath(p.path)) for p in self.pages}

    # -------------------------------------------------------- crop to all
    def apply_crop_to_all(self, source: PageEdit) -> int:
        """Push `source`'s crop onto every other page.

        Called only when you ask for it, by name: this used to run after every
        crop edit while a project-wide "link" was switched on, which meant a
        careful nudge on one page was silently copied over all the others.  Now
        it is what the "crop all pages" tool does and nothing else.

        A "locked" page is skipped in both directions - it neither receives the
        crop nor pushes its own onto anyone.  That exemption is what stops a
        spread alignment from being destroyed by a crop somewhere else.

        An "own_position" page keeps its x/y but still takes the width and
        height, because "every page the same size" is the promise that should
        not break.
        """
        if source.crop is None:
            return 0
        if source.crop_mode == "locked":
            return 0
        x, y, w, h = source.crop
        n = 0
        for p in self.pages:
            if p is source or p.crop_mode == "locked":
                continue
            if p.crop and p.crop_mode == "own_position":
                new = [p.crop[0], p.crop[1], w, h]
            else:
                new = [x, y, w, h]
            if p.crop != new:
                p.crop = new
                n += 1
        return n

    def locked_pages(self) -> list:
        return [p for p in self.pages if p.crop_mode == "locked"]

    # ------------------------------------------------------- cutting / joining
    def replace(self, uid: str, pages: list) -> bool:
        """Swap one page for the pages it became, keeping its place in the queue."""
        i = self.index_of(uid)
        if i < 0:
            return False
        self.pages[i:i + 1] = list(pages)
        return True

    # -------------------------------------------------------- crop size matching
    def size_groups(self, tolerance: int = 5, include_locked: bool = False) -> list:
        """Cluster the pages by output size, within `tolerance` pixels.

        Scans of the same document rarely come out to the same number of pixels
        - the sheet sits a hair differently on the glass each time, and a crop
        drawn by hand lands a few pixels off.  Grouping by "close enough" is
        what turns that into something you can act on: here are eleven pages
        that all meant to be 4960x7016, nudge them onto it.

        Returns [{"size": (w, h), "pages": [uid, ...], "spread": (dw, dh)}, ...],
        biggest group first.
        """
        items = [(p, p.output_size()) for p in self.pages
                 if p.src_w and (include_locked or p.crop_mode != "locked")]
        groups: list = []
        for page, size in sorted(items, key=lambda it: (-it[1][0], -it[1][1])):
            for g in groups:
                if (abs(size[0] - g["size"][0]) <= tolerance
                        and abs(size[1] - g["size"][1]) <= tolerance):
                    g["pages"].append(page.uid)
                    g["sizes"].append(size)
                    break
            else:
                groups.append({"size": size, "pages": [page.uid], "sizes": [size]})
        for g in groups:
            ws = [s[0] for s in g["sizes"]]
            hs = [s[1] for s in g["sizes"]]
            g["spread"] = (max(ws) - min(ws), max(hs) - min(hs))
            # The group's nominal size is the size most of its pages already
            # are - not whichever page happened to be visited first, and not an
            # average, which would move every page including the ones that were
            # right.  With no majority, the middle value: it is a real size some
            # page has, and it is the same answer every time.
            best = max(set(g["sizes"]), key=g["sizes"].count)
            if g["sizes"].count(best) > 1:
                g["size"] = best
            else:
                g["size"] = (sorted(ws)[len(ws) // 2], sorted(hs)[len(hs) // 2])
        groups.sort(key=lambda g: -len(g["pages"]))
        return groups

    def match_sizes(self, uids: list, size: tuple, anchor: str = "center") -> int:
        """Force the given pages to an exact output size.

        The crop is grown or shrunk about its own centre by default, so the
        content stays where it was framed instead of sliding toward a corner.
        """
        w, h = int(size[0]), int(size[1])
        if w < 1 or h < 1:
            return 0
        wanted = set(uids)
        n = 0
        for p in self.pages:
            if p.uid not in wanted:
                continue
            r = p.frame_rect()
            if anchor == "center":
                x = r.x + (r.w - w) / 2.0
                y = r.y + (r.h - h) / 2.0
            else:
                x, y = r.x, r.y
            new = [x, y, float(w), float(h)]
            if p.crop != new:
                p.crop = new
                n += 1
        return n

    def uniform_size(self) -> Optional[tuple]:
        """The one output size among the pages that a crop-all reaches.

        Locked pages are excluded on purpose: a spread-aligned pair is meant to
        differ, so counting it would make the queue permanently report "sizes
        differ" and the warning would stop meaning anything.
        """
        sizes = {p.output_size() for p in self.pages
                 if p.src_w and p.crop_mode != "locked"}
        return sizes.pop() if len(sizes) == 1 else None

    # ------------------------------------------------------------- reordering
    def move(self, uids: list, delta: int) -> bool:
        """Shift the given pages up or down one slot, keeping their order."""
        idx = sorted(i for i, p in enumerate(self.pages) if p.uid in set(uids))
        if not idx:
            return False
        if delta < 0 and idx[0] == 0:
            return False
        if delta > 0 and idx[-1] == len(self.pages) - 1:
            return False
        for i in (idx if delta < 0 else reversed(idx)):
            j = i + delta
            self.pages[i], self.pages[j] = self.pages[j], self.pages[i]
        return True

    def move_to(self, uids: list, position: int) -> bool:
        """Move the given pages so the first of them sits at 1-based `position`.

        The pages keep their order relative to each other, and `position` is
        read against the final list - "move to 3 of 40" puts it third.
        """
        picked = set(uids)
        moving = [p for p in self.pages if p.uid in picked]
        if not moving:
            return False
        rest = [p for p in self.pages if p.uid not in picked]
        at = max(0, min(len(rest), int(position) - 1))
        self.pages = rest[:at] + moving + rest[at:]
        return True

    def reorder_visible(self, new_order: list) -> None:
        """Rewrite the order of a subset, leaving pages not in it where they are.

        The slots those pages currently occupy are refilled in the given order,
        so reordering a filtered view never disturbs the pages you cannot see.
        """
        wanted = set(new_order)
        slots = [i for i, p in enumerate(self.pages) if p.uid in wanted]
        lookup = {p.uid: p for p in self.pages}
        for slot, uid in zip(slots, new_order):
            page = lookup.get(uid)
            if page is not None:
                self.pages[slot] = page

    def sort_pages(self, key: str = "name", reverse: bool = False) -> None:
        import re as _re

        def natural(p: PageEdit):
            parts = _re.split(r"(\d+)", p.name.lower())
            return [int(t) if t.isdigit() else t for t in parts]

        if key == "name":
            self.pages.sort(key=natural, reverse=reverse)
        elif key == "modified":
            self.pages.sort(key=lambda p: os.path.getmtime(p.path)
                            if os.path.exists(p.path) else 0, reverse=reverse)
        elif key == "size":
            self.pages.sort(key=lambda p: os.path.getsize(p.path)
                            if os.path.exists(p.path) else 0, reverse=reverse)
        elif key == "reverse":
            self.pages.reverse()

    def suggested_name(self) -> str:
        """A sensible document name.

        The scan folder wins over the project file name: a folder called
        "Sony_KV-16G1" is what you meant, whereas the project may well have
        been saved as "scanforge.sfproj".  Getting that precedence backwards is
        why the name only sometimes came out right.
        """
        if self.document_name:
            return self.document_name
        if self.last_folder:
            name = os.path.basename(os.path.normpath(self.last_folder))
            if name:
                return name
        if self.path:
            return os.path.splitext(os.path.basename(self.path))[0]
        return "pages"

    def default_export_dir(self) -> str:
        """Alongside the scan folder, not inside it."""
        if not self.last_folder:
            return ""
        parent = os.path.dirname(os.path.normpath(self.last_folder))
        return parent or self.last_folder

    def clear(self) -> None:
        """Reset to an empty project, keeping nothing from the last one."""
        self.pages = []
        self.path = ""
        self.export_dir = ""
        self.last_folder = ""
        self.document_name = ""

    # ------------------------------------------------------------------- I/O
    def save(self, path: str) -> None:
        data = {
            "schema": SCHEMA_VERSION,
            "export_dir": self.export_dir,
            "last_folder": self.last_folder,
            "document_name": self.document_name,
            "pages": [p.to_dict() for p in self.pages],
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
        self.path = path

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        proj = cls(path=path)
        proj.export_dir = data.get("export_dir", "")
        proj.last_folder = data.get("last_folder", "")
        # `link_crop` used to live here: a project-wide switch that copied
        # every crop edit onto every page.  Two crop tools replaced it, so the
        # key is read from older project files and discarded.
        proj.document_name = data.get("document_name", "")
        if not proj.document_name:
            # projects from before the name became project-wide kept it per page
            for d in data.get("pages", []):
                if d.get("document"):
                    proj.document_name = d["document"]
                    break
        proj.pages = [PageEdit.from_dict(d) for d in data.get("pages", [])]
        return proj
