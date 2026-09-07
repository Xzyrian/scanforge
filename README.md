# ScanForge

A crop / straighten / align bench for high-resolution archival scans. Built for
600 dpi TIFFs of vintage brochures and manuals, where a page is 10000 × 14000 px
and general-purpose editors start swimming.

---

## Why it isn't slow

GIMP and Paint.NET are slow on these files because they want to touch all 140
million pixels every time you drag something. ScanForge never does.

* **Each source file is decoded exactly once.** Three reduced copies (320 px,
  2200 px, 5200 px) are written to a cache under `%LOCALAPPDATA%\ScanForge`.
  Re-opening a page after that is a ~50 ms JPEG decode, not a 10-second TIFF
  decode.
* **All editing happens on the 2200 px proxy**, and rotation is a Qt transform
  on a pixmap rather than a resample. Dragging the angle slider redraws in
  single-digit milliseconds whether the source is 4 MP or 140 MP.
* **Nothing slow ever runs on the interface thread.** Decoding a page, probing
  a folder of headers, measuring skew, finding the sheet, loading two pages for
  a spread — all of it happens on worker threads and reports back when done.
* **Thumbnails are drawn for the rows you can see, and no others.** A thumbnail
  is a JPEG decode, a scale and a paint — about three milliseconds — and the
  queue used to build every one of them while assembling the list. On a
  300-page folder that was a full second with the window frozen, and on a big
  one several; it happened at the end of every import and again on every sort,
  filter and undo. Measured: **1258 ms → 11 ms** to put 300 rows up, with the
  pictures filling in afterwards in slices of a few milliseconds. That, rather
  than the decoding, is what "it stops responding while it caches" was.
* **One slow thing at a time.** If you trigger another while one is running, it
  is refused with a note in the status bar rather than stacked on top. That
  combination — work on the GUI thread plus a second action landing inside the
  first — is what used to freeze the app outright.
* **The whole folder is prepared before you can touch it.** Dropping a folder
  puts up a modal window *first* — before the queue is filled and before a
  single file is opened — listing every page with its own bar: waiting,
  reading, ready. It covers the whole import, headers and pixels both, and the
  app is unavailable until the last page lands. There is no close button, no
  Cancel and no Escape: half a loaded queue is exactly what people used to poke
  at, and poking at it is what wedged the app. Being modal is also what makes
  the wait *safe*, because Qt runs the event loop with input blocked, so the
  window keeps painting while no click can reach the half-built project
  underneath. There is one exception to "no way out", and it is not a
  convenience: a dialog you cannot close is only defensible while finishing is
  guaranteed, and nothing that reads a disk is guaranteed. So it watches its own
  progress, and if nothing has moved for ninety seconds it says so and offers to
  carry on with whatever loaded. That button cannot be reached while pages are
  still arriving. **Cancel** is separate and always available: it abandons the
  import outright and takes the pages back out of the project.
* **The percentage on each page is real** — it counts segments actually
  decoded. A compressed TIFF is walked strip by strip or tile by tile, so there
  is something honest to report several times a second rather than an animation
  that only means "still working". A file with nothing to count — a single-strip
  TIFF, a JPEG — gets an indeterminate bar instead, which is the honest thing to
  show when nothing can be measured.
* **The bar does not fill before the work is done.** The decode owns the first
  88% and writing the cache files owns the rest. Encoding three tiers of JPEG
  for a 140 MP page is around half a second, and it used to happen with the bar
  already sitting on 100%, which looks exactly like a hang.
* **The route through the file is chosen per file, not fixed.** Measured on a
  108 MP page, warm cache, best of three:

  | layout | average into the small array | paste, resize once | read whole |
  |---|---|---|---|
  | tiled Deflate | 0.97 s / 312 MB | **0.65 s** / 566 MB | 0.66 s / 565 MB |
  | striped Deflate | 0.83 s / 311 MB | **0.57 s** / 565 MB | 0.59 s / 563 MB |
  | striped LZW | 2.52 s / 398 MB | 3.10 s / 650 MB | **2.51 s** / 648 MB |
  | uncompressed | 0.89 s / 686 MB | 0.53 s / 890 MB | **0.21 s** / 397 MB |

  So an uncompressed TIFF is read whole — `tifffile` maps it straight off the
  disk, which is four times faster *and* smaller than walking it. Anything
  compressed is walked, because that is what makes the percentage possible; each
  segment is pasted into a full-size array when there is memory to spare, and
  averaged into the small one when there is not — 45% less memory for about 40%
  more time. The earlier version always did the averaging, which is why importing
  got slower than it needed to be.
* **Running out of memory does not fall back to the greediest path.** A walk that
  hits `MemoryError` drops the decode limit and retries the *smaller* walk. It
  used to fall through to reading the whole file, which is the one thing that
  cannot possibly work when memory has already run out — and because the failure
  was swallowed, the page was silently decoded twice.
* **`tools/decode_report.py` says what actually happened** to a given scan: how
  it is stored, which route was picked and why, where the percentage paused and
  for how long, and — with `--all` — every route timed on that file. It is the
  thing to run when a bar behaves oddly, because the answer is usually specific
  to the file rather than to the program.
* **When something is running, it says so**: a spinner in the status bar, and a
  panel over the canvas naming what is being waited on.
* **Full resolution is touched once, at export**, in a worker pool with a
  progress window you can cancel.
* **The crop is resolved before the rotation.** Only the pixels that survive
  into the final page get resampled — rotating the whole frame and then
  discarding a third of it would cost several seconds and about a gigabyte per
  page.
* **Each scan is decoded exactly once on import.** The queue wants a thumbnail
  and the canvas wants a proxy; those used to be two jobs, so every 140 MP TIFF
  in the folder was decoded twice. Now one decode writes both tiers, and
  overlapping requests for the same file merge into one job.
* **Only so many decodes at a time, whichever part of the app asked.** Three
  separate thread pools can each decide to decode a scan, and none of them knew
  about the others; six concurrent decodes is around six gigabytes of buffers,
  at which point the machine swaps, and if it runs out the application dies
  part-way through your import. One gate in front of the decode holds the line,
  sized from the free memory on the machine and the actual dimensions of the
  scans — which is why nothing is decoded until the headers have been read, and
  why the sizing is deliberately timid: a slower import costs you a minute, a
  crashed one costs you the whole import.

Measured on a 2-core container (your machine will be quicker):

| Operation | Time |
|---|---|
| First open of a 10000 × 14000 TIFF (builds the proxy pyramid) | 1.5 s |
| Re-open from cache | 50 ms |
| Auto-deskew | 0.24 s |
| Detect page edges | < 0.5 s |
| Full-res render + write of one 140 MP page (Lanczos, ZIP) | 12–15 s |

Importing three 140 MP scans, same machine, before and after the decode work:

| | Time | Peak memory |
|---|---|---|
| Two jobs per file, threaded decode, three at once | 15.8 s | 2.7 GB |
| One job per file, gated | 10.1 s | 0.97 GB |

Choosing the route per file rather than always averaging into the small array,
importing three 108 MP scans on the same container:

| Source | Always averaging | Route chosen |
|---|---|---|
| Tiled Deflate | 3.50 s / 376 MB | 3.13 s / 629 MB |
| Uncompressed | 2.99 s / 751 MB | **0.81 s / 459 MB** |

The uncompressed row is where it matters — nearly four times faster on *less*
memory. For a compressed scan the decode itself gets about 30% quicker but the
decode is only a third of an import, so the end-to-end gain is around 10%, paid
for in memory; that is why it is conditional on there being memory to spare.

The memory column is the one that matters: 2.7 GB of decode buffers for three
pages is what pushes a real machine into swap, and swapping is what "it lags a
ton while importing" actually feels like.

---

## Setup

1. Install Python 3.10 or newer from python.org, ticking **Add python.exe to
   PATH**.
2. Double-click **`setup.bat`**. It builds a private `.venv` inside the app
   folder and installs PySide6, OpenCV, tifffile, imagecodecs, NumPy and Pillow.
   Nothing is installed system-wide.
3. Double-click **`run.bat`**.

`run-console.bat` does the same but leaves a console window open, which is what
you want if something goes wrong. `run-tests.bat` runs the full test suite.

---

## Building an executable to share

Run **`build-exe.bat`**. Eight steps, in this order:

1. Warn if two OpenCV wheels are installed (that alone adds ~150 MB).
2. Bring `.venv` up to date with `requirements.txt`, and install PyInstaller.
3. **Self-test the source tree first.** Two minutes here beats ten minutes of
   PyInstaller followed by the discovery that the source was already broken.
4. Draw the application icon.
5. **Delete `build\` and `dist\`.** Without this a failed build leaves the last
   good exe sitting in `dist\`, and the steps below test and zip *that* — which
   is how a stale build gets shipped.
6. Build.
7. **Run the built executable's own self-test.** If a codec or a Qt piece
   failed to get bundled, the build is reported as broken rather than handed to
   you looking fine. The exe is a windowed build with no console to print to, so
   it writes its report to `dist\selftest.log` and the script prints that.
8. Zip it.

You get `dist\ScanForge\ScanForge.exe` plus `dist\ScanForge-windows.zip`.
Whoever you send it to unzips it and runs the .exe — no Python, no install.

Arguments, in any combination:

| Argument | Effect |
| --- | --- |
| `onefile` | One self-contained `ScanForge.exe` instead of a folder. Smaller to send, but it unpacks itself into a temp folder on every launch, so it starts several seconds slower. The folder build is the better default. |
| `quick` | Skip step 3. Only worth it when you have just run `run-tests.bat`. |
| `nopause` | Do not wait for a keypress at the end, for calling it from another script. |

Expect roughly 300 MB unpacked, most of it Qt, OpenCV and the TIFF codecs.
Nothing is stripped that the app can actually reach: the codec set is kept whole
so it can still open oddly-compressed vintage TIFFs.

### The self-test

`ScanForge.exe --selftest` runs it any time, and `python tools\freeze_check.py`
runs the same checks against the source tree (`run-tests.bat` does this as its
third step). It checks numpy, OpenCV, every TIFF compression the export dialog
offers, Pillow, PDF assembly, the full render pipeline (crop + rotate + blur),
joined pages, page detection, the lazily-imported dialogs, and that the window
and all its icons actually build.

The report is always written to a file as well as printed, because a windowed
PyInstaller build has no stdout — `print` goes nowhere — which made the one
moment the report mattered most the moment it was invisible. The path is
`%TEMP%\scanforge-selftest.log`, or wherever `SCANFORGE_SELFTEST_LOG` points.

This exists because a frozen build fails in a specific way: everything imports
perfectly from source, then the .exe runs and some C extension PyInstaller never
noticed is missing. It caught two real ones while this was being written — the
TIFF codecs and Pillow's JPEG encoder, the latter of which would have failed
only when someone got as far as exporting a PDF.

### If Windows SmartScreen complains

The .exe is unsigned, so Windows will warn the first time it runs, and so will
the people you share it with ("More info" → "Run anyway"). Signing needs a
code-signing certificate, which costs money. UPX compression is deliberately
turned off in the build spec, since packed Qt DLLs make antivirus far more
suspicious than the size saving is worth.

---

## Getting scans in

**Drag a folder onto the window — anywhere in it.** The page itself, the page
grid, the tool column, the chrome around them: all of it is a drop target. It
used to be only the page grid, because a `QGraphicsView` switches drops on for
its own viewport so it can hand them to items in the scene, and the canvas was
accepting folders and then silently discarding them — which is unfortunate,
since the middle of the window is exactly where you aim.

That folder becomes the working folder: it
sets the document name, seeds the file dialogs, and puts the default export
destination **beside** the scan folder rather than inside it. Dropping loose
files works too. Dropping a `.sfproj` opens that project.

If a project is already open, dropping another folder asks whether to **start a
new project** or **add to this one** — it never silently merges two documents.
**Ctrl+N** starts fresh at any time.

`Ctrl+O` does the same thing through a folder picker, and you can drag a folder
onto `run.bat` to open it at launch.

---

## How the window is laid out

Left to right, in the order the work flows:

```
 ┌─────────────────────────────────────────────────────────────┐
 │  open save save-as export │ undo redo │ guides │ grid book  │
 ├──────┬───────────────────────────────────┬──────────────────┤
 │      │                                   │                  │
 │ tool │          the scan                 │   page grid      │
 │ col  │                                   │                  │
 │      ├───────────────────────────────────┼──────────────────┤
 │      │  angle  0.000°  0  ──────●──────  │   what is left   │
 │      │                                   │   of the panel   │
 ├──────┴───────────────────────────────────┴──────────────────┤
 │  status · page 3 of 240 · x,y · [fit] ────●──── 61%         │
 └─────────────────────────────────────────────────────────────┘
```

The tools act on the page, so they sit beside it. The page grid moved to the
right, where you look to see where you are in the document rather than what you
are doing to one sheet of it. The panel under the grid is a **holding position**
— crop numbers, the reuse buttons and the blur controls have not been given
homes yet, and it goes when they have. It scrolls rather than pushing the grid
out of the column, and the grid keeps at least two rows of pages whatever the
panel would like.

The tool column sits outside the splitter: it is a fixed width, and a divider
beside something that cannot be resized is a control that does nothing.

---

## The top bar and the tool column

**Top bar** — open scan folder · save project · save project as · export ¦
undo · redo ¦ guide ― · guide │ · clear guides ¦ show grid · preview as a book.

That is the document and the view: what you are working on, and how you are
looking at it. Nothing in the top bar changes a page.

**Status bar, far right** — a **Fit** button and a **zoom slider** with the
percentage beside it, where a zoom control is always looked for. The bar used to
carry three readouts and nothing you could operate.

There is no **100%** and no **50%**. Everything on screen is a 2200 px
interpretation of a 10000 px scan, so "actual pixels" names something the editor
never shows you; the number was a fiction and the two buttons spent toolbar
space on it. The slider is logarithmic — the useful range runs from a whole
140 MP page in a window (around 6%) up to a few hundred percent, and a linear
slider spends nine tenths of its travel above 100% where nothing is ever set.

**Tool column** — a column between the page grid and the canvas, two icons
abreast, with a rule between each group:

```
  cursor   crop page     the tools; one is armed at a time
  crop all blur
  cut
  ────────────────
  rot ←    rot →         orientation, for a scan that arrived sideways
  flip ⇄   flip ⇅
  ────────────────
  link     copy          framing, which reaches past this page
  match    spread
  join
  ────────────────
  auto     fit page      the automatic pass, and finding the sheet
  ────────────────
  done                   finish this page and move on
```

The tools act on the page in front of you, so they sit beside it. A bar across
the top of the window puts the crop tool as far from the crop as the layout
allows, and that is a trip your eye makes on every page of a two-hundred-page
manual. Standing the bar on its end was not enough on its own — a `QToolBar`
is only one button wide that way, and sixteen tools in single file is taller
than most windows — hence two columns, which also lets the grouping show as
whitespace and a rule rather than a separator you have to squint at.

The grid is half-inch, drawn from the crop's own corner, and it is drawn at two
screen pixels rather than one. It used to be a single hairline at alpha 55,
which against a scan of white paper was invisible — and a measuring aid you
cannot see is worse than none, because you believe you checked. Measured against
the old pen on a rendered page, it now lays down six times the ink.

Everything is a square icon; hover for the name and shortcut.

---

## Cropping

Two tools, and the difference between them is how far the rectangle reaches:

* **Crop page (C)** — crops the page in front of you and nothing else.
* **Crop all pages (Shift+C)** — draws one rectangle and gives it to every
  page in the project.

Both draw the same way, both release themselves after one drag, and **neither
arms itself**. A new project opens on the cursor.

A page you have not cropped shows a crop window around the whole sheet, with
handles on its corners and edges, so cropping usually starts by pulling that
frame in rather than drawing a rectangle from nothing. Drag an edge and the page
is cropped — that is the crop-page action, done with the cursor.

### What this replaced, and why

There used to be a project-wide switch — *Same crop for every page* — that was
**on by default**, so the first crop you drew went to the whole document and the
tool armed itself when a project opened. It then switched itself off, on the
theory that the first crop answers "what shape is this document" and later
edits are per-page.

Every part of that was a rule you had to know. The switch changed what a drag
meant without being where you were looking; the arming decided for you what the
first action would be; and the release depended on a piece of state that had to
be re-derived after every undo, because a remembered flag did not survive one.
Two tools say the same thing by name, at the moment you pick one, with nothing
to remember and nothing happening behind you.

Per-page settings still decide who *receives* a crop-all:

* **Follows** — takes the whole rectangle. The default.
* **Keeps its own position** — takes the width and height but stays where it
  sits, so the pages match in size without moving the content.
* **Locked** — sits out entirely, in both directions: it neither receives a
  crop-all nor pushes its own onto anyone. Spread-aligned pages, cut halves and
  joined pages are locked automatically, so a crop-all cannot destroy an
  alignment you spent time on.

A click without a drag does nothing — it will not commit a one-pixel crop, to
this page or to two hundred of them.

### Adjusting a crop once it is there

Drag its handles in cursor mode. Arrow keys nudge by exactly 1 source pixel —
Shift for 10, Alt to resize instead of move. The X/Y/W/H boxes are in
full-resolution source pixels, and the Result panel shows the exact output size
in pixels, inches and millimetres.

**Fit to page edges (E)** finds the sheet against the scanner bed and pulls the
crop just inside it. It deliberately trims a hair inside the paper edge, because
a bounding box around a slightly skewed sheet always catches wedges of bed in
the corners. Like every crop edit, it acts on the page it is run on.

If you rotate a page after cropping, the crop follows the content rather than
staying put in frame coordinates.

---

## Tools

There are four, and only one is ever active:

* **Cursor (V)** — the resting state. Pan, zoom, and drag the handles of a crop
  you already have. It **cannot** start a new rectangle, so a stray click on the
  page can't wipe out framing you spent time on.
* **Crop (C)** — draw one new rectangle. The moment you release the mouse it
  hands control back to the cursor, so you have to arm it deliberately each
  time.
* **Blur (B)** — drag out a region to obscure. Also one-shot.
* **Cut (X)** — split this scan into separate pages along a line you place.

**Esc** returns to the cursor from anywhere.

---

## Blurring a spot

Press **B** and drag over whatever you want gone — a previous owner's name, a
price sticker, a serial number. Click without dragging to drop one at a default
size instead.

Afterwards it behaves like the crop: in cursor mode you drag it around, pull its
handles to resize, nudge it with the arrow keys, and **Delete** removes it. The
inspector has a strength slider and a choice of **Blur** or **Pixelate**.

What you see on the canvas is a real blur of the actual pixels, not a grey box
standing in for one, so you can tell whether it is strong enough before you
export. A page can carry as many as you like, they follow the content when you
straighten the page, and they are undoable like everything else.

---

## One document, many pages

The whole queue is treated as pages of a single document — a brochure or a
manual — not a folder of unrelated files. So there is one document name, set at
export time (or with **Ctrl+G**), and **Crop all pages** to give them one shape.

The queue footer confirms it — "all 9300×13304" — and turns to "sizes differ" if
anything has drifted apart. For crops that are a few pixels out rather than
wrong, **Match crop sizes** (Ctrl+Shift+M) is the gentler tool: it brings them
onto one exact size without moving anything.

Each page chooses how a crop-all treats it, in the panel:

* **Follows** — takes the whole rectangle. The default.
* **Keeps its own position** — for a sheet that sat differently on the glass. It
  keeps its own x/y but still takes the width and height, so sizes match.
* **Locked — never overwritten** — completely exempt, in both directions.

**Spread-aligned pages are locked automatically.** Their framing was set against
their facing page, and a crop-all must not come along later and replace it.
Locked pages show a 🔒 in the queue, and are excluded from the "all 9300×13304"
size check since they are meant to differ.

---

## Straightening to 0.01°

* **Guides (Space, Shift+Space)** — level and upright lines to judge the page
  against, described in their own section below. Most precise, usually fastest.
* **Auto-deskew (D)** — projection-profile estimator over the text lines. Sweeps
  0.1° steps then refines at 0.01°, resolving to about ±0.01° on real text.
* **The slider** — spans ±2.000° across its full width, so one screen pixel is
  roughly 0.01°.
* **`[` and `]`** — 0.01° per press, 0.10° with Shift.

The angle box takes three decimals directly if you already know the number.

---

## Guides

**Space** and **Shift+Space**, or the three buttons in the top bar, drop a cyan
line across the middle of the view. Drag it to a baseline, a margin
or a spine, and then turn the *page* onto *it* with `[` and `]`, the nudge
buttons, or the angle box. **Clear** takes them all off.

They are the same guides the spread aligner uses, which is the point: judging
one page and judging two facing pages are the same task, and they used to be
two different tools.

* A guide appears in the middle of **what you are looking at**, not the middle
  of the page — zoomed in on a headline, a guide placed at the page's centre
  lands off screen and has to be hunted for. A second guide steps a little
  clear of the first so it is not hidden underneath it.
* Guides are level or upright by construction, so there is nothing to get
  wrong about them. They stay put when you change pages, so a whole document
  can be squared against one reference.
* They belong to the page's coordinates, so they stay on the feature you put
  them on as you pan and zoom.

### What happened to the straightedge

There used to be a free-angle straightedge (**L**) that you traced along a
crooked feature, plus **Ctrl+L** to rotate the page onto it. Both are gone.

The straightedge could be laid down at any angle, which meant it could be laid
down *wrong* — and a reference line that is itself a fifth of a degree out
aligns the page to its own error, invisibly. It tried to protect against that by
snapping to level within 1.5°, at which point it was a level line with extra
steps. A guide that is level by construction cannot be off, and the judgement it
supports — does this baseline sit on this line — is the same one.

---

## Cutting one scan into several pages

A fold-out photographed in one pass, two pages caught on one sheet of glass, a
strip of three panels: press **X** and click where it should be divided. Hold
**Shift** to cut across instead of down. The status bar shows both resulting
sizes, in pixels and inches, as you move.

For even divisions, **Page ▸ Cut into pages** has halves, thirds and quarters,
horizontally or vertically — one action, not one per line.

What you get is *n* pages in the queue over the same source file, each with its
own narrower crop. Nothing is written to disk and the source is never modified;
a cut costs no space and undo puts it straight back. The pieces are **locked**
against a crop-all, because a cut page is deliberately not the shape
of the rest of the document. Each keeps its own thumbnail, cropped to the piece,
and its own name suffix (`scan_004 · left`) so the two halves are never confused
in the queue or at export.

---

## Joining two scans into one page

The inverse: a page too big for the platen, scanned in two overlapping passes.
Select both, **Ctrl+Shift+J**, and they open in the same alignment view the
spread uses — nudge, rotate, match heights, set the gap. Then **Join into one
page**.

The seam is wherever the two crops meet, so *trim each half's inner edge before
joining* and the overlap disappears. The result is a single page in the queue
whose own crop then trims the assembled image; it exports as one file and goes
into the PDF as one page.

The two source files are still untouched on disk — the join is a description of
how to assemble them, not a merged copy. **Page ▸ Split a joined page apart**
returns the two scans with the alignment they had.

---

## Matching crop sizes

**Ctrl+Shift+M.** Scans of one document never come out to the same number of
pixels: the sheet sits a hair differently on the glass every time, and a
hand-drawn crop lands a few pixels off. Eleven pages at 4960×7016 and three at
4958×7014 is a difference you cannot see and a PDF reader certainly can.

The dialog groups the pages by "within a few pixels of each other" — the
tolerance is yours to set, 5 px by default — and shows each group with how much
it actually varies. Pick one, nudge the target size up or down a pixel at a time
if you want, and press **Match**.

Every crop in the group is resized **about its own centre**, so the sizes change
and the framing does not move. Locked pages (spreads, cut pieces, joined pages)
sit out unless you tick them in, since they are meant to be a different shape.

The changes are small by nature — that is the point of the tool — so the status
bar says how many pages actually moved and by how much, rather than how many
were in the group. "All 14 pages were already 4960×7016" and "12 of 14 pages
resized — the largest change was 2 px" are different outcomes, and both used to
report the same thing, which made a tool that was working look like one that
was not. The pages that moved also drop their cached thumbnails, so the grid
redraws them instead of showing the old framing until you happen to scroll.

---

## Managing the queue

Raw scans rarely come off the scanner in reading order, so the list is editable.

Each page carries its **position in the bottom-right corner of the scan**, in a
dim block, the way a page number sits on a printed page. There is no file name
under it: every scan in a project comes from one folder and is named after it,
so four hundred repetitions of that tell you nothing. The line of text below a
page is reserved for what has been **done** to it — *edited*, *locked*,
*spread*, *joined*, *left* / *right* for the halves of a cut scan, *done* in
green. A page nobody has touched says nothing at all, which is what makes the
ones that do say something easy to find.

* **Ctrl-click and Shift-click** pick several pages. There is no sweep
  selection — dragging is for reordering, and one gesture cannot mean two
  things on the same button.
* **Drag** to reorder. The moment you drag far enough the pages you picked up
  are **lifted out of the list**, a faded copy of them follows the cursor, and a
  blank slot opens where they will land. The other pages shuffle around the
  slot, and where the slot is when you let go is exactly where the pages go — no
  caret to interpret, no guessing. Multi-selections move together and keep their
  own relative order; the copy under the cursor draws as a small stack with the
  count on it. Dragging near the top or bottom scrolls, and **Esc** puts
  everything back.

  The slot's position is worked out from the grid arithmetic, not by asking what
  is under the cursor. The items move as the slot moves, so a hit test gave an
  answer that depended on where the slot already was — it could shift one way,
  change the layout, and read back the other way on the next movement. That is
  the drift, and arithmetic on a uniform grid cannot do it.

  This deliberately does not use the system's drag and drop. Handing the job to
  the platform meant the drop was resolved somewhere that could not be seen or
  tested — it passed a synthetic test and kept failing in the actual
  application. A reorder is now an ordinary mouse gesture on the list, which can
  be driven end to end by the test suite. Dropping *files* from Explorer is
  still real drag and drop; that is the only thing the platform is asked to do.
* **The panel snaps to whole columns.** Drag the divider and it settles on the
  nearest column boundary, because a panel a column and a bit wide wastes the
  bit — the grid cannot use it and the pages do not grow into it.
* **Ctrl+M** — *move to position*. Type any number; if it is out of range the
  dialog says so in red and will not confirm until it is valid, rather than
  silently clamping what you typed to something else.
* **Ctrl+Shift+↑ / ↓** move the selected pages, or use the arrow buttons under
  the list.
* **Delete** removes pages from the queue. *The files on disk are never
  touched.*
* **Sort button** — by file name (natural, so `page_2` sorts before `page_10`),
  by date modified, by size, or reverse the current order.
* **Right-click** for document name, align as spread, **mark done / not done**,
  move to top/bottom, reveal in Explorer, and remove.
* **Enter** marks the selection done; **Ctrl+Enter** flips it either way. There
  was no way to un-finish a page at all: Enter only ever set the mark, and
  taking it back meant finding the page again and unticking a box in the
  inspector, one page at a time — a box which, alone among the edits in the
  app, undo could not reach either. Both are fixed.
* **The divider between the grid and the canvas is ten pixels wide** and the
  panels cannot be collapsed to nothing. Dragging that divider is the only way
  to change the number of columns, and at the default four or five pixels it was
  a game of patience. (`QSplitter.restoreState` carries the handle width with
  it, so a session saved before this quietly put the thin one back — the width
  is now re-applied after the restore.)

Reordering works with a filter active too — it permutes the visible pages among
the slots they already occupy and leaves hidden pages where they are.

---

## Reading it as a book

**Ctrl+P.** Everything else here looks at one page at a time, which is right for
cropping and wrong for answering "is this actually correct?". Facing pages that
are each perfectly square can still disagree with each other, and a page that is
upside down, out of order or missing is invisible one page at a time.

So this opens a separate window showing the finished pages the way a reader
meets them: two at a time, in order, with a gutter between them and a page turn
when you move. What it draws is the *rendered* page — orientation, straightening,
crop and blurs all applied, exactly what the exporter will write — so it is a
check on the product, not on the source.

* **Click a page to turn it** — the left page goes back, the right page goes on,
  split at the spine, so the half you clicked is the half you were reading.
  Arrows and PageUp/PageDown do the same. The wheel turns pages; **Ctrl+wheel**
  zooms; **Home** and **End** jump to the ends; the slider scrubs.
* **The turn is animated as one sheet**: its front closes onto the spine, then
  its back opens out the other side, 420 ms end to end. Jumping more than one
  spread — the slider, Home, End — cuts straight there instead, because an
  animation between pages that aren't adjacent is a lie about the document.
* **The book is fitted once, not per spread.** The scale comes from the largest
  pair in the document — the widest left page beside the widest right one, and
  the tallest page — so every spread is drawn at the same size and the book
  doesn't breathe as you turn it. Nothing overflows, whatever the window shape,
  and a fold-out that is twice the width of its neighbours simply sets the
  scale for everything. The spine stays at the centre of the window.
* **First page is a cover** is on by default, because a bound document opens
  with page one alone on the right. Turn it off for loose sheets or a folded
  leaflet and the pairs start from page one.
* **The covers stand where a book puts them.** Page one sits on the *right* of
  the spine with its left edge against the gutter — the whole block is still
  under it, so it has nowhere else to be. The back cover has nothing left
  beneath it, so the book eases sideways until that page is in the middle of
  the window: it travels there over the length of the turn rather than
  jumping, which is what tells you you have reached the end.
* It reads the 2200 px proxies, and fetches the detail tier on a worker thread
  once you zoom far enough in for the difference to show — the same bargain the
  editing canvas makes.

---

## Aligning facing pages

Select a page and press **Ctrl+J**. If you only have one selected, a panel takes
over the canvas telling you to click the facing page in the queue, with a Cancel
button. Esc cancels too.

It opens **in the main working area**, in place of the editing canvas, with the
queue and the inspector still beside it — it used to be a floating window, which
put the alignment somewhere other than where the work happens. Esc leaves it.

While it is open, **every page but the two being aligned is greyed out and
cannot be selected**, and the inspector is disabled. Both were live before, and
both were traps: clicking a third page in the queue loaded it into the inspector
while the aligner went on editing the original pair, so the panel on the right
described a page that was not on screen and any adjustment made there landed on
the wrong one.

The queue also keeps its width. The aligner's control strip is a row of group
boxes whose combined minimum width used to set the whole pane's minimum, which
the splitter then took out of the page grid — squeezing it to a single column,
and leaving it there after the aligner closed, saved into the next session. The
strip now sits in a scroll area and asks for nothing, and the divider positions
are restored on the way out.

The two scans are butted together at the gutter so you see the spread as the
reader would. Then:

* Drag either page to slide it under its own crop window.
* **Tab** switches which side is active; arrow keys nudge it by a pixel.
* **`[` `]`** rotate the active page by 0.01°.
* **Add ―** drops a horizontal guide that runs across *both* pages. That's the
  tool that does the job: put a guide on a baseline on the left page and see
  where it lands on the right.
* **Match Height / Width / Both**, and **Align tops** — one-shot, driven by the
  active page.
* **Link** keeps them matched. Both crop frames carry handles; drag either one
  by an edge or a corner and the other follows. **Size only** — each page keeps
  its own position over its own scan, because a spread is made consistent by
  making the two pages the same shape, not by dragging one onto the other.
  Switching the link on matches them straight away from the active page, rather
  than waiting for the next drag and leaving the spread mismatched while
  claiming to be joined up.

  This is what a bound spread usually needs: the two halves were scanned
  separately, the sheet sat differently on the glass each time, and the finished
  pages still have to be the same size or the book reads as if it were assembled
  from offcuts. The frames touch at the gutter, so their handles overlap there —
  the active page gets first claim on its own edge.
* **Show beyond crop** ghosts in the area outside each crop.

"Keep alignment" writes each page's own crop and angle and records that the two
are a pair. **They stay two separate files.** The spread is a viewing aid, not
an output format — nothing is ever merged on disk.

---

## Undo and redo

**Ctrl+Z** and **Ctrl+Y** (or Ctrl+Shift+Z). They're application-wide shortcuts,
so a focused spin box can't swallow them.

Everything is undoable, including the batch operations: applying framing across
a document, sorting, reordering, removing pages, linking crops, a spread
alignment. Drags and slider scrubs coalesce into a single history entry rather
than hundreds. The undo button's tooltip names what it will undo.

---

## Export and naming

The naming section is driven by a preset dropdown with a live preview of the
actual file names you'll get, so there's nothing to decode.

For `Sony_KV-16G1_0001.tif` the setup is:

1. Type `Sony_KV-16G1` into **Document name** at the top of the export dialog
   (or set it beforehand with **Ctrl+G**). It is the stem for both the pages and
   the PDF.
2. Leave the rest alone — the defaults are already this.

You get:

```
…\edited\Sony_KV-16G1\
    Sony_KV-16G1_0001.tif
    Sony_KV-16G1_0002.tif
    …
…\edited\
    Sony_KV-16G1.pdf
```

Page numbers count from 1 within each document, not from 0. The PDF always takes
the document name alone, with no page number. If you want a different shape, the
**Pattern** box takes `{doc}`, `{page}`, `{name}` (original file name) and
`{index}` (unpadded number).

Defaults: **no TIFF compression** (archival masters first — turn on Deflate if
you would rather have the disk space), **no subfolder**, dash naming, and a PDF
at **whatever resolution the scans themselves are**. A 600 dpi PDF of a long
manual gets very large, so drop that number if you want something to email.

**A zip of the exported pages** is produced by default, named
`<document>-images.zip`, holding the image files flat with no folder structure
and *not* the PDF.

**The individual page files are not kept, by default.** The project file already
records every crop, angle and blur against the untouched scans, so the cropped
pages are usually a step on the way to the zip or the PDF rather than the thing
you wanted — and a set of 600 dpi TIFFs is several gigabytes to leave lying
about for nothing to read. Tick **Keep the individual page files as well** when
you do want them.

The zip and the PDF are both assembled from files on disk, so this cannot mean
"don't write them": the pages are written to a temporary folder inside the
destination (same filesystem, so nothing is copied across devices), bundled, and
then taken away. The export still reports every page it wrote, and the run reads
identically either way. With neither the zip nor the PDF ticked the option is
greyed out and the pages are kept regardless, because otherwise the export would
produce nothing at all.

Other options: TIFF compression (None / LZW / Deflate / Packbits / JPEG), bit
depth, resolution (full, target DPI, or percent), and a JPEG preview beside each
TIFF. Bit depth, channel count and DPI carry across from the source; pages over
30 MP are written tiled so other software opens them quickly.

**Parallel workers** defaults to 2. Each worker holds a full page in memory —
about 420 MB for 8-bit RGB at this size, 840 MB for 16-bit — so 2 or 3 is the
sweet spot unless you have a lot of RAM. **Lanczos** is sharpest and is the
default; **Bicubic** is roughly 3× faster and very hard to tell apart on a
600 dpi scan of print, which makes it reasonable for a large batch.

---

## Keyboard reference

Press **F1** in the app for this list.

```
TOOLS
  V                   cursor - pan, zoom, adjust an existing crop or blur
  C                   crop this page - one drag, then it releases
  Shift+C             crop every page in the project - same, but for all
  B                   blur - drag out a blur over something on the page
  X                   cut - split this scan into separate pages
  Del                 delete the selected blur
  Esc                 back to the cursor

NAVIGATION
  Ctrl+←  Ctrl+→      previous / next page
  Enter               mark done and go to next
  Ctrl+Enter          mark done / not done - the inverse of Enter
  wheel               scroll down the page
  Shift+wheel         scroll across
  Ctrl+wheel          zoom at the cursor
  Ctrl++  Ctrl+-      zoom in / out
  middle-drag         pan
  F                   fit page

STRAIGHTENING
  D                   auto-deskew from the text lines
  [  ]                rotate by 0.01°     Shift+[ ]   by 0.10°
  Space  Shift+Space  drop a level / upright guide to judge the page against

CUTTING AND JOINING
  X                   place a cut line - Shift cuts across instead of down
  Page > Cut          measured cuts: halves, thirds, quarters
  Ctrl+Shift+J        join two scans into a single page

CROPPING
  arrows              nudge 1 px   (Shift = 10 px, Alt = resize)
  E                   fit crop to the detected page edges
  A                   auto straighten and crop
  Ctrl+D              clear crop

QUEUE
  Delete              remove selected pages (files on disk are untouched)
  drag                reorder
  Ctrl+Shift+↑ ↓      move selected pages up / down
  Ctrl+M              move selected pages to a chosen position
  right-click         document name, spread, move to position/top/bottom, reveal

BATCH
  Ctrl+B              copy framing from previous page
  Ctrl+Shift+B        apply this framing to selected pages
  Ctrl+G              set the document name
  Ctrl+J              align two pages as a spread
  Ctrl+Shift+M        match crops that are within a few pixels of each other
  Ctrl+E              export

HISTORY
  Ctrl+Z  Ctrl+Y      undo / redo
```

---

## How it's put together

```
scanforge/
  geometry.py      rotation maths and the coordinate spaces - the contract
                   the preview and the exporter both obey
  model.py         PageEdit / Project, the non-destructive edit record,
                   crop propagation, blur regions, cutting and joining,
                   size grouping, reordering and sorting
  history.py       undo/redo by whole-project snapshot
  imageio_util.py  TIFF read/write preserving bit depth, channels, DPI, ICC
  proxycache.py    the three-tier proxy pyramid and its disk cache
  render.py        full-resolution render (crop-before-rotate), and the
                   assembly of a joined page from its parts
  autodetect.py    page-edge detection, projection-profile deskew
  export.py        batch runner, naming, PDF assembly
  ui/
    icons.py       every icon and the app mark, drawn from code - no assets
    loader.py      background decoding and one-off worker tasks, so nothing
                   blocks the interface
    canvas.py      the editing canvas, the tools, the guides
    inspector.py   the right-hand controls
    queue_panel.py the page list
    spread_view.py facing-page alignment, in the work area
    book_view.py   read the finished pages two at a time
    loading_dialog.py the modal that owns the window while a queue is read
    size_match_dialog.py grouping near-identical crops onto one exact size
    export_dialog.py
    main_window.py
tests/
  test_pipeline.py geometry, render, detection, export, on synthetic 600 dpi scans
  test_ui.py       headless widget test - builds the window, drives it, paints it
tools/
  freeze_check.py  the self-test that runs inside the built executable
  make_icon.py     writes the multi-resolution .ico for the build
scanforge.spec     PyInstaller build definition
scanforge_app.py   entry point the frozen build is built around
```

The piece worth knowing if you want to modify it: **the canvas scene coordinate
system *is* the rotated frame**, at proxy scale. The crop rectangle you drag
needs no translation to become the crop the exporter applies — it's the same
rectangle, divided by the proxy scale factor. `geometry.rotation_matrix()` is
the single definition of that frame, and both the Qt preview transform and the
OpenCV export warp are built from it, which is why they can't drift apart.

Angle convention: **positive is clockwise**, matching Qt. The auto-detect
functions return the *correction* to apply, so their result goes straight into
`PageEdit.angle`.

### Tests

`run-tests.bat`. The pipeline suite builds a real 10000 × 14000 scan and checks,
among other things, that the fast crop-before-rotate path is pixel-identical to
the naive rotate-then-slice path (mean difference around 0.01 grey levels, i.e.
interpolation noise), that a known 0.37° skew is recovered exactly, that 16-bit
and greyscale survive the round trip, and that the size shown in the inspector
is the size that lands on disk. It also cuts a two-colour sheet in half and
checks the left page is genuinely the left of the scan, joins two scans and
exports the assembled page, and checks that a joined page and a cut piece both
survive a save and reload with their alignment intact.

The UI suite drives the same features through the widgets: that the alignment
guide stays put and keeps its angle while the page rotates under it, that
drawing it does *not* rotate the page, that a cut in the canvas produces two
pages accounting for the whole scan, and that undo puts the scan back together.

The UI suite builds the real window offscreen and drives it: that a stray drag
in cursor mode can't destroy a crop, that the crop tool releases itself, that a
crop change reaches every page, that undo restores a removed page and a
whole-project change, that reordering and sorting behave, that
`Sony_KV-16G1_0001.tif` and `Sony_KV-16G1.pdf` are what the naming produces, and
that the spread view puts two pages edge to edge.

---

## Notes and limits

* Reads TIFF, PNG, JPEG, JP2, BMP, WebP. Multi-page TIFFs are read as their
  first page only.
* ICC profiles are preserved on export for files under 400 MB; above that the
  profile is dropped rather than pay for a full rewrite.
* The proxy cache grows at roughly 3–4 MB per page. Clear it from
  **Tools → Clear proxy cache**; it rebuilds on demand.
* Removing a page from the queue never touches the file on disk.
* Projects autosave every 45 seconds once you've saved them once.
* If a source file moves after being added, the project tells you which ones it
  can no longer find when you reopen it.
