"""Say exactly what happens when ScanForge reads a scan.

Point it at the files that behave oddly:

    python tools/decode_report.py "A:\\Scans\\brochure\\*.tif"

For each one it prints how the file is stored, which of the three decode
routes gets picked and why, and - the part worth having - the progress
timeline: where the percentage pauses, for how long, and what it was doing.

That last column is the answer to "the bar stopped at 62%".  A pause is
either work that is genuinely happening (a slow segment, a machine short of
memory) or a route that failed part-way and started again, and the report
tells the two apart.  `--all` additionally times every route on the same file
so the choice can be checked rather than trusted.
"""

from __future__ import annotations

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanforge import imageio_util as iio           # noqa: E402
from scanforge import proxycache as pc              # noqa: E402


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}"
        n /= 1024.0
    return f"{n:.0f} GB"


def report(path: str, every_route: bool) -> None:
    print("=" * 72)
    print(os.path.basename(path))
    lay = pc._layout(path)
    size = os.path.getsize(path)
    if lay is None:
        print(f"  not walkable segment by segment - it will be read whole"
              f"   ({human(size)} on disk)")
    else:
        print(f"  {lay['W']} x {lay['H']} x {lay['C']}  {lay['dtype']}   "
              f"{lay['segments']} segments   {human(size)} on disk, "
              f"{human(lay['stored'])} decoded"
              + ("   uncompressed" if lay["raw"] else ""))
    plan = pc._plan(path, pc.PROXY, lay)
    print(f"  free memory {human(pc.available_memory())}  ->  route: {plan}")

    marks = []
    t0 = time.time()
    pc._decode(path, pc.PROXY, lambda f: marks.append((time.time() - t0, f)))
    total = time.time() - t0
    took, why = pc.last_strategy.get(os.path.basename(path), (plan, ""))
    print(f"  decoded in {total:.2f}s via {took}" + (f" — {why}" if why else ""))

    if not marks:
        print("  no progress was reported at all")
    else:
        gaps = [(marks[i][0] - (marks[i - 1][0] if i else 0.0), marks[i - 1][1]
                 if i else 0.0) for i in range(len(marks))]
        worst = sorted(gaps, reverse=True)[:3]
        print(f"  {len(marks)} progress reports, ending at "
              f"{marks[-1][1] * 100:.0f}%")
        for gap, at in worst:
            if gap < 0.05:
                continue
            print(f"     paused {gap * 1000:5.0f} ms while the bar showed "
                  f"{at * 100:3.0f}%")
        if marks[-1][1] < 0.999:
            print("     *** the bar never reached the end: the route gave up "
                  "part-way and the file was decoded again from scratch")

    if every_route:
        print("  same file by every route:")
        for route in ("accumulate", "paste", "whole"):
            os.environ["SCANFORGE_DECODE"] = route
            best = 1e9
            for _ in range(2):
                t = time.time()
                try:
                    out = pc._decode(path, pc.PROXY)
                except Exception as exc:                 # noqa: BLE001
                    print(f"     {route:11s} failed: {exc}")
                    out = None
                    break
                best = min(best, time.time() - t)
            if out is not None:
                print(f"     {route:11s} {best:6.2f}s")
        os.environ.pop("SCANFORGE_DECODE", None)


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    every = "--all" in argv
    paths = []
    for a in args:
        hits = glob.glob(a)
        paths.extend(hits or [a])
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        print(__doc__)
        return 2
    print(f"tifffile {getattr(iio.tifffile, '__version__', '?')}   "
          f"decode gate {pc._gate.limit}")
    for p in paths:
        report(p, every)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
