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
* **The percentage on each page is real.** A tiled TIFF is decoded a tile at a
  time, so there is something honest to report several times a second rather
  than an animation that only means "still working". Reading it that way also
  turned out to be *faster* — 2.3 s against 3.5 s on a 140 MP scan, and 637 MB
  peak against 964 MB, because the full-size array is never built at all: each
  tile is reduced into the small one and dropped. Files that are not tiled
  TIFFs fall back to the plain path and an indeterminate bar, which is the
  honest thing to show when nothing can be measured.
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

Run **`build-exe.bat`**. It installs PyInstaller into the same `.venv`, draws
the application icon, builds, and — the part that matters — **runs the built
executable's own self-test before declaring success**. If a codec or a Qt piece
failed to get bundled, the build is reported as broken rather than handed to you
looking fine.

You get `dist\ScanForge\ScanForge.exe` plus `dist\ScanForge-windows.zip`.
Whoever you send it to unzips it and runs the .exe — no Python, no install.

`build-exe.bat onefile` produces a single self-contained `ScanForge.exe`
instead. It is smaller to send but unpacks itself into a temp folder on every
launch, so it starts several seconds slower. The folder build is the better
default; onefile is for when you need to hand over exactly one file.

Expect roughly 300 MB unpacked, most of it Qt, OpenCV and the TIFF codecs.
Nothing is stripped that the app can actually reach: the codec set is kept whole
so it can still open oddly-compressed vintage TIFFs.

### The self-test

`ScanForge.exe --selftest` runs it any time. It checks numpy, OpenCV, every TIFF
compression the export dialog offers, Pillow, PDF assembly, the full render
pipeline (crop + rotate + blur), page detection, and that the window and all its
icons actually build.

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

**Drag a folder onto the window.** That folder becomes the working folder: it
sets the document name, seeds the file dialogs, and puts the default export
destination **beside** the scan folder rather than inside it. Dropping loose
files works too. Dropping a `.sfproj` opens that project.

If a project is already open, dropping another folder asks whether to **start a
new project** or **add to this one** — it never silently merges two documents.
**Ctrl+N** starts fresh at any time.

`Ctrl+O` does the same thing through a folder picker, and you can drag a folder
onto `run.bat` to open it at launch.

---

## The two tool bars

**Top bar** — open scan folder · save project · save project as · export ¦
undo · redo ¦ show grid.

**Second bar** — selection tools: cursor · crop · alignment guide · blur · cut ¦
page tools: link crops · copy framing · align spread ¦ auto tools: auto ·
auto-deskew · fit to page edges ¦ zoom: fit · 100% ¦ mark done.

Everything is a square icon; hover for the name and shortcut.

---

## The first crop

A new project opens with the **crop tool already armed**. Drag out the crop for
the document, it lands on every page at once, and then **both** the crop tool
and the project-wide crop release themselves. That is the whole first step.

The project-wide crop exists to answer one question — what shape is this
document — and it answers it the moment you draw that first rectangle. Leaving
it switched on afterwards is what made careful per-page work disappear: every
later nudge on any page was quietly copied over all the others. So it switches
itself off, and the status bar says so. Turn it back on from the **link** button
on the tool bar whenever you actually want a change to reach every page.

A click without a drag does nothing — it will not commit a one-pixel crop to the
entire project.

---

## Tools

There are five, and only one is ever active:

* **Cursor (V)** — the resting state. Pan, zoom, and drag the handles of a crop
  you already have. It **cannot** start a new rectangle, so a stray click on the
  page can't wipe out framing you spent time on.
* **Crop (C)** — draw one new rectangle. The moment you release the mouse it
  hands control back to the cursor, so you have to arm it deliberately each
  time.
* **Alignment guide (L)** — lay a straightedge across the page. It stays there;
  see below.
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

## One document, one crop

The whole queue is treated as pages of a single document — a brochure or a
manual — not a folder of unrelated files. So there is one document name, set at
export time (or with **Ctrl+G**), and one crop shared by every page.

The crop link is on when a project opens, because a scanned document is one
object photographed many times and every page of an archival set should come out
the same size. It turns **itself off** once that first crop has been applied to
everything (see above), so the rest of your session is per-page by default.

While it is on, changing the crop anywhere lands it on every page in the
project. The queue footer confirms it — "all 9300×13304" — and turns to "sizes
differ" if anything has drifted apart.

The **link** button on the tool bar switches it on and off at any time. For
crops that have already drifted a few pixels apart, **Match crop sizes**
(Ctrl+Shift+M) is the gentler tool: it brings them onto one exact size without
moving anything.

Each page also chooses how it follows the shared crop, in the inspector:

* **Follows the shared crop** — the default.
* **Keeps its own position** — for a sheet that sat differently on the glass. It
  keeps its own x/y but still takes the shared width and height, so sizes match.
* **Locked — never overwritten** — completely exempt, in both directions.

**Spread-aligned pages are locked automatically.** Their framing was set against
their facing page, and the shared crop must not come along later and replace it.
That is exactly what used to happen: aligning a spread, then nudging the crop on
any unrelated page, silently destroyed the alignment. Locked pages show a 🔒 in
the queue, and are excluded from the "all 9300×13304" size check since they are
meant to differ.

---

## Straightening to 0.01°

* **Alignment guide (L)** — a straightedge you turn the page against, described
  in its own section below. Most precise, usually fastest.
* **Auto-deskew (D)** — projection-profile estimator over the text lines. Sweeps
  0.1° steps then refines at 0.01°, resolving to about ±0.01° on real text.
* **The slider** — spans ±2.000° across its full width, so one screen pixel is
  roughly 0.01°.
* **`[` and `]`** — 0.01° per press, 0.10° with Shift.

The angle box takes three decimals directly if you already know the number.

---

## The alignment guide

Press **L** and drag out a line. It stays on the canvas, thick and cyan, with a
faint dashed extension running the width of the view — and then you turn the
*page* onto *it*, with `[` and `]`, the nudge buttons, or the angle box.

This is the reverse of the usual "draw a line and the page snaps to it", and the
reason is that snapping gives you one shot: if the line was a hair off, so is
the page, and you have nothing left on screen to check against. A straightedge
that stays put is something you can judge against repeatedly, at any zoom, along
its whole length.

* A guide drawn within 1.5° of level or plumb **snaps to exactly level or
  plumb**, because a straightedge that is itself a fifth of a degree out is
  worse than none. Hold **Alt** while drawing to keep a free angle — for
  aligning to a diagonal.
* **Shift** while drawing constrains to horizontal or vertical outright.
* In cursor mode, drag the guide by its middle to move it, or by either end to
  re-aim it.
* It stays put when you change pages, so a whole document can be squared against
  one reference.
* **Ctrl+L** rotates the page so the guide's own slope becomes level — the old
  behaviour, kept for when you traced the guide along something crooked rather
  than laying it down as a reference. **Shift+L** clears the guide.

The guide belongs to the **editor window, not to the page**. It is a
straightedge held against the screen: rotating, panning or zooming moves the
page underneath while the guide stays exactly where you put it, holding both its
position and its angle. A guide that drifted with the page could never converge
with it.

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
against the project-wide crop, because a cut page is deliberately not the shape
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

---

## Cropping

Arm the crop tool and drag; afterwards, adjust with the handles in cursor mode.
Arrow keys nudge by exactly 1 source pixel — Shift for 10, Alt to resize instead
of move. The X/Y/W/H boxes are in full-resolution source pixels, and the Result
panel shows the exact output size in pixels, inches and millimetres.

**Fit to page edges (E)** finds the sheet against the scanner bed and pulls the
crop just inside it. It deliberately trims a hair inside the paper edge, because
a bounding box around a slightly skewed sheet always catches wedges of bed in
the corners.

If you rotate a page after cropping, the crop follows the content rather than
staying put in frame coordinates.

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
* **Right-click** for document name, align as spread, move to top/bottom, reveal
  in Explorer, and remove.

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

It opens **in the main working area**, in place of the editing canvas, with the queue and the inspector still beside it — it used to be a floating window, which put the alignment somewhere other than where the work happens. Esc leaves it.

The two scans are butted together at the gutter so you see the spread as the
reader would. Then:

* Drag either page to slide it under its own crop window.
* **Tab** switches which side is active; arrow keys nudge it by a pixel.
* **`[` `]`** rotate the active page by 0.01°.
* **Add ―** drops a horizontal guide that runs across *both* pages. That's the
  tool that does the job: put a guide on a baseline on the left page and see
  where it lands on the right.
* **Match Height / Width / Both**, and **Align tops**.
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
and *not* the PDF. The loose files are kept as well; the zip is there so you
have one thing to send.

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
  C                   crop - draw ONE new crop, then it releases
  L                   alignment guide - lay down a straightedge that stays put
  B                   blur - drag out a blur over something on the page
  X                   cut - split this scan into separate pages
  Del                 delete the selected blur
  Esc                 back to the cursor

NAVIGATION
  Ctrl+←  Ctrl+→      previous / next page
  Enter               mark done and go to next
  wheel               zoom at the cursor
  middle-drag, Space  pan
  F                   fit page          1 / 2   100% / 50%

STRAIGHTENING
  D                   auto-deskew from the text lines
  [  ]                rotate by 0.01°     Shift+[ ]   by 0.10°
  Ctrl+L              rotate the page so the guide becomes level
  Shift+L             clear the guide

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
                   crop linking, blur regions, cutting and joining,
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
    canvas.py      the editing canvas, the tools, the alignment guide
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
