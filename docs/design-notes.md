# Design notes

Why ScanForge is built the way it is. Almost every entry here was written
against a specific failure — something that froze, lied, or quietly did the
wrong thing — so each one says what the behaviour replaced as well as what it
is. The [README](../README.md) covers what the application does; this covers
why.

> **This is slopcode.** The application was written by an LLM, essentially in
> full — see [the note in the README](../README.md#this-is-slopcode). These
> notes are the reasoning behind the decisions, written down as they were made.
> They are worth reading if you want to understand or change the program. They
> are not a claim that the code implementing any of it has been reviewed by a
> human, because it has not.

---

## Contents

- [Speed and memory](#speed-and-memory)
- [The import](#the-import)
- [Layout](#layout)
- [Cropping](#cropping)
- [Guides, and the straightedge they replaced](#guides-and-the-straightedge-they-replaced)
- [The page grid](#the-page-grid)
- [Blurring](#blurring)
- [Matching crop sizes](#matching-crop-sizes)
- [Facing pages](#facing-pages)
- [The book view](#the-book-view)
- [Export](#export)
- [Dialogs](#dialogs)
- [The build and its self-test](#the-build-and-its-self-test)

---

## Speed and memory

GIMP and Paint.NET are slow on these files because they want to touch all 140
million pixels every time you drag something. ScanForge never does.

**Each source file is decoded exactly once.** Three reduced copies (320 px,
2200 px, 5200 px) are written to a cache under `%LOCALAPPDATA%\ScanForge`.
Re-opening a page after that is a ~50 ms JPEG decode, not a 10-second TIFF
decode.

**All editing happens on the 2200 px proxy**, and rotation is a Qt transform on
a pixmap rather than a resample. Dragging the angle slider redraws in
single-digit milliseconds whether the source is 4 MP or 140 MP.

**Nothing slow ever runs on the interface thread.** Decoding a page, probing a
folder of headers, measuring skew, finding the sheet, loading two pages for a
spread — all of it happens on worker threads and reports back when done.

**The crop is resolved before the rotation.** Only the pixels that survive into
the final page get resampled; rotating the whole frame and then discarding a
third of it would cost several seconds and about a gigabyte per page.

**Full resolution is touched once, at export**, in a worker pool with a progress
window you can cancel.

**One slow thing at a time.** Trigger another while one is running and it is
refused with a note in the status bar rather than stacked on top. That
combination — work on the GUI thread plus a second action landing inside the
first — is what used to freeze the app outright.

### Thumbnails are drawn for the rows you can see

A thumbnail is a JPEG decode, a scale and a paint: about three milliseconds. The
queue used to build every one of them while assembling the list, which on a
300-page folder was a full second with the window frozen, and on a big one
several. It happened at the end of every import and again on every sort, filter
and undo.

Measured: **1258 ms → 11 ms** to put 300 rows up, with the pictures filling in
afterwards in slices of a few milliseconds. That, rather than the decoding, is
what "it stops responding while it caches" actually was.

### One decode per scan, and a gate in front of it

The queue wants a thumbnail and the canvas wants a proxy. Those used to be two
jobs, so every 140 MP TIFF in the folder was decoded twice. Now one decode
writes both tiers, and overlapping requests for the same file merge into one
job.

Three separate thread pools could each decide to decode a scan, and none of them
knew about the others. Six concurrent decodes is around six gigabytes of
buffers, at which point the machine swaps — and if it runs out, the application
dies part-way through your import. One gate in front of the decode holds the
line, sized from the free memory on the machine and the actual dimensions of the
scans. That is why nothing is decoded until the headers have been read, and why
the sizing is deliberately timid: a slower import costs you a minute, a crashed
one costs you the whole import.

Importing three 140 MP scans on a 2-core container:

| | Time | Peak memory |
|---|---|---|
| Two jobs per file, threaded decode, three at once | 15.8 s | 2.7 GB |
| One job per file, gated | 10.1 s | 0.97 GB |

The memory column is the one that matters. 2.7 GB of decode buffers for three
pages is what pushes a real machine into swap, and swapping is what "it lags a
ton while importing" actually feels like.

### The route through the file is chosen per file

Measured on a 108 MP page, warm cache, best of three:

| layout | average into the small array | paste, resize once | read whole |
|---|---|---|---|
| tiled Deflate | 0.97 s / 312 MB | **0.65 s** / 566 MB | 0.66 s / 565 MB |
| striped Deflate | 0.83 s / 311 MB | **0.57 s** / 565 MB | 0.59 s / 563 MB |
| striped LZW | 2.52 s / 398 MB | 3.10 s / 650 MB | **2.51 s** / 648 MB |
| uncompressed | 0.89 s / 686 MB | 0.53 s / 890 MB | **0.21 s** / 397 MB |

So an uncompressed TIFF is read whole — `tifffile` maps it straight off the
disk, which is four times faster *and* smaller than walking it. Anything
compressed is walked, because walking is what makes an honest percentage
possible; each segment is pasted into a full-size array when there is memory to
spare, and averaged into the small one when there is not — 45% less memory for
about 40% more time.

The earlier version always did the averaging, which is why importing got slower
than it needed to be. End to end, importing three 108 MP scans:

| Source | Always averaging | Route chosen |
|---|---|---|
| Tiled Deflate | 3.50 s / 376 MB | 3.13 s / 629 MB |
| Uncompressed | 2.99 s / 751 MB | **0.81 s / 459 MB** |

The uncompressed row is where it matters — nearly four times faster on *less*
memory. For a compressed scan the decode itself gets about 30% quicker, but the
decode is only a third of an import, so the end-to-end gain is around 10% paid
for in memory. That is why it is conditional on there being memory to spare.

**Running out of memory does not fall back to the greediest path.** A walk that
hits `MemoryError` drops the decode limit and retries the *smaller* walk. It
used to fall through to reading the whole file, which is the one thing that
cannot possibly work when memory has already run out — and because the failure
was swallowed, the page was silently decoded twice.

`tools/decode_report.py` says what actually happened to a given scan: how it is
stored, which route was picked and why, where the percentage paused and for how
long, and with `--all`, every route timed on that file. It is the thing to run
when a bar behaves oddly, because the answer is usually specific to the file
rather than to the program.

---

## The import

**The whole folder is prepared before you can touch it.** Dropping a folder puts
up a modal window *first* — before the queue is filled and before a single file
is opened — listing every page with its own bar: waiting, reading, ready. It
covers the whole import, headers and pixels both, and the app is unavailable
until the last page lands.

There is no close button, no Cancel and no Escape. Half a loaded queue is
exactly what people used to poke at, and poking at it is what wedged the app.
Being modal is also what makes the wait *safe*: Qt runs the event loop with
input blocked, so the window keeps painting while no click can reach the
half-built project underneath.

There is one exception to "no way out", and it is not a convenience. A dialog
you cannot close is only defensible while finishing is guaranteed, and nothing
that reads a disk is guaranteed — so it watches its own progress, and if nothing
has moved for ninety seconds it says so and offers to carry on with whatever
loaded. That button cannot be reached while pages are still arriving. **Cancel**
is separate and always available: it abandons the import outright and takes the
pages back out of the project.

**The percentage on each page is real.** It counts segments actually decoded. A
compressed TIFF is walked strip by strip or tile by tile, so there is something
honest to report several times a second rather than an animation that only means
"still working". A file with nothing to count — a single-strip TIFF, a JPEG —
gets an indeterminate bar instead, which is the honest thing to show when
nothing can be measured.

**The bar does not fill before the work is done.** The decode owns the first
88% and writing the cache files owns the rest. Encoding three tiers of JPEG for
a 140 MP page is around half a second, and it used to happen with the bar
already sitting on 100%, which looks exactly like a hang.

**Drops are accepted anywhere in the window.** It used to be only the page grid.
A `QGraphicsView` switches drops on for its own viewport so it can hand them to
items in the scene, so the canvas was accepting folders and then silently
discarding them — unfortunate, since the middle of the window is exactly where
you aim. Every drop target now routes through one method, which also means
dropping a `.sfproj` opens it rather than adding it to the queue as if it were a
scan.

---

## Layout

The tools act on the page in front of you, so they sit beside it. A bar across
the top of the window puts the crop tool as far from the crop as the layout
allows, and that is a trip your eye makes on every page of a two-hundred-page
manual.

Standing the bar on its end was not enough on its own — a `QToolBar` is only one
button wide that way, and sixteen tools in single file is taller than most
windows. Hence two icons abreast, which also lets the grouping show as
whitespace and a rule rather than a separator you have to squint at.

The tool column sits **outside** the splitter: it is a fixed width, and a
divider beside something that cannot be resized is a control that does nothing.

The page grid moved to the right, where you look to see where you are in the
document rather than what you are doing to one sheet of it, and it has the
whole of that column.

### The options bar, and the panel it replaced

For a while there was a panel under the page grid, left over from the old
inspector. By the end it was mostly repeats — draw a new crop, fit to page
edges, blur a spot and copy framing were all buttons in the tool column too —
its result readout was clipped at the edge ("Physi… 3.533 × 5.083 in (89.7 ×"),
and with no project open it was a wall of disabled controls. What was *not* a
repeat moved into one strip above the canvas: the crop numbers and what they
export as, the aspect lock, what crop-all does to this page, the angle, and the
selected blur. Numbers for the page belong next to the page.

The strip wraps rather than pushing the page grid aside in a narrow window, and
it leaves with the canvas when a mode takes the work area. It used to be the
angle strip alone under the canvas, and it stayed live while the aligner was
open — the slider went on turning the page hidden behind it, the same trap the
keyboard shortcuts had been. The handlers now refuse edits while a mode is
open as well, so it does not matter how a change arrives.

### Names on hover, descriptions in the status bar

Every button's tooltip is now its name and key, and the status bar says what
it does. They used to be the same text — sometimes the name, sometimes a
sentence — so hovering told you one or the other depending on the button.
Stripping `&` from menu text to make the name also turned "Auto straighten &&
crop" into "Auto straighten  crop"; a doubled ampersand is a literal one.

### The first screen

With nothing open, the window used to show one line of grey text in the
middle of an empty canvas, framed by a disabled tool column, a disabled panel
and an empty grid — a first screen made of things you could not use yet. It is
a start screen now: what the app is for, the two ways in, recent projects, and
one line on how the work goes. The tools, save and export stay off until there
is a page; undo does not, because removing every page is exactly when you might
want it back.

### One theme

Colours were written out wherever they were needed: twenty-nine style sheets,
the same secondary-text grey typed a dozen times, and a few near-greys that
were meant to be the same one. They are named tokens in `theme.py` now, with
one application style sheet built from them, and secondary text is marked as
such (`theme.hint`) rather than coloured by hand.

### The status bar

It used to carry three readouts and nothing you could operate. It now has a
**Fit** button and a **zoom slider** with the percentage beside it, at the far
right, where a zoom control is always looked for.

There is no **100%** and no **50%**. Everything on screen is a 2200 px
interpretation of a 10000 px scan, so "actual pixels" names something the editor
never shows you; the number was a fiction and the two buttons spent toolbar
space on it.

The slider is logarithmic. The useful range runs from a whole 140 MP page in a
window (around 6%) up to a few hundred percent, and a linear slider spends nine
tenths of its travel above 100% where nothing is ever set.

### The grid overlay

Half-inch, drawn from the crop's own corner, at two screen pixels rather than
one. It used to be a single hairline at alpha 55, which against a scan of white
paper was invisible — and a measuring aid you cannot see is worse than none,
because you believe you checked. Measured against the old pen on a rendered
page, it now lays down six times the ink.

---

## Cropping

There used to be a project-wide switch — *Same crop for every page* — that was
**on by default**, so the first crop you drew went to the whole document and the
crop tool armed itself when a project opened. It then switched itself off, on
the theory that the first crop answers "what shape is this document" and later
edits are per-page.

Every part of that was a rule you had to know. The switch changed what a drag
meant without being where you were looking; the arming decided for you what your
first action would be; and the release depended on a piece of state that had to
be re-derived after every undo, because a remembered flag did not survive one.

Two tools — **Crop page** and **Crop all pages** — say the same thing by name,
at the moment you pick one, with nothing to remember and nothing happening
behind you. Neither arms itself; a new project opens on the cursor.

A click without a drag does nothing. It will not commit a one-pixel crop, to
this page or to two hundred of them.

The cursor tool deliberately **cannot** start a new rectangle, so a stray click
on the page can't wipe out framing you spent time on. Handles on an existing
crop are still live in cursor mode, which is what makes pulling a frame in the
normal way to crop.

**An uncropped page shows nothing.** It used to be drawn with a frame around
the whole sheet, handles and all, which is indistinguishable from a full-page
crop somebody chose — "not cropped yet" and "cropped to the edges" looked
identical, and the invisible handles meant a click near the edge silently
created a crop nobody asked for. Now a page nobody has cropped simply looks
uncropped, and nothing on it is grabbable.

**A click with the crop tool takes the whole page**, handles and all, ready to
pull in. That is how most pages are actually framed, and it used to require a
drag from one corner of the scan to the other before you could start. A click
used to do nothing at all, on the reasoning that it must not commit a
one-pixel crop — true, but "do nothing" was not the only other option.

**Editing the crop across the document is a toggle — but a visible one.**
Crop-all sets one rectangle on every page; it had no way to *adjust* that
rectangle afterwards short of drawing it again. *Edit all pages with this crop*
brings the ability back, and it deliberately differs from the old
*Same crop for every page* switch in three ways, each aimed at that switch's
failure — an edit reaching pages you did not know it would reach:

- it is **off** until you turn it on, and off again every time the app starts;
- while it is on, the crop frame is **amber and says how many other pages** the
  edit will reach, on the frame itself rather than in a panel;
- it reaches only pages whose crop is the **same size** as this one, and
  applies each edge's movement from each page's own position.

The size rule is what makes it survive straightening. After a crop-all the
rectangles are identical, but straightening a page moves its crop a few pixels
to follow the content — matching on the exact rectangle would have dropped
every straightened page out of the group, which after a crop-all is every page.
Straightening itself never propagates, since it is about one page's scan.

**A crop stays inside the scan.** Past the edge is scanner bed, or nothing, and
it is a slip of the hand far more often than an intention: drawing clamps at
the edge as you drag, and moving an existing crop slides it back in at the same
size rather than shrinking it. Two things are still allowed out, because both
are deliberate — holding Shift while drawing, and straightening, which moves
the crop with the content and must not have its answer clipped underneath it.
When a locked aspect ratio meets the edge, the clamp shrinks the longer side to
keep the shape rather than breaking it.

**Fit to page edges** trims a hair inside the paper edge on purpose: a bounding
box around a slightly skewed sheet always catches wedges of scanner bed in the
corners.

---

## Guides, and the straightedge they replaced

There used to be a free-angle straightedge (**L**) that you traced along a
crooked feature, plus **Ctrl+L** to rotate the page onto it. Both are gone.

The straightedge could be laid down at any angle, which meant it could be laid
down *wrong* — and a reference line that is itself a fifth of a degree out
aligns the page to its own error, invisibly. It tried to protect against that by
snapping to level within 1.5°, at which point it was a level line with extra
steps. A guide that is level by construction cannot be off, and the judgement it
supports — does this baseline sit on this line — is the same one.

The guides are also the ones the spread aligner uses, which is the point:
judging one page and judging two facing pages are the same task, and they used
to be two different tools.

A guide appears in the middle of **what you are looking at**, not the middle of
the page. Zoomed in on a headline, a guide placed at the page's centre lands off
screen and has to be hunted for. A second guide steps a little clear of the
first so it is not hidden underneath it.

---

## The page grid

**Reordering does not use the system's drag and drop.** Handing the job to the
platform meant the drop was resolved somewhere the application could not see
into: it behaved correctly in isolation and kept failing in use, with no way to
observe why. A reorder is now an ordinary mouse gesture on the list, which the
application handles itself and can therefore inspect. Dropping *files* from
Explorer is still real drag and drop — that is the only thing the platform is
asked to do.

When you drag far enough, the pages you picked up are lifted out of the list, a
faded copy follows the cursor, and a blank slot opens where they will land.
Where the slot is when you let go is exactly where the pages go: no caret to
interpret, no guessing.

The slot's position is worked out from the grid arithmetic, not by asking what
is under the cursor. The items move as the slot moves, so a hit test gave an
answer that depended on where the slot already was — it could shift one way,
change the layout, and read back the other way on the next movement. That is the
drift, and arithmetic on a uniform grid cannot do it.

**There is no sweep selection.** Dragging is for reordering, and one gesture
cannot mean two things on the same button.

**The panel snaps to whole columns**, because a panel a column and a bit wide
wastes the bit: the grid cannot use it and the pages do not grow into it.

**The divider is ten pixels wide** and the panels cannot be collapsed to
nothing. Dragging that divider is the only way to change the number of columns,
and at Qt's default four or five pixels it was a game of patience.
`QSplitter.restoreState` carries the handle width with it, so a session saved
before this quietly put the thin one back — the width is now re-applied after
the restore.

**Ctrl+M refuses an out-of-range position in red** rather than silently clamping
what you typed to something else.

**Un-finishing a page was impossible.** Enter only ever *set* the done mark, and
taking it back meant finding the page again and unticking a box in the
inspector, one page at a time — a box which, alone among the edits in the app,
undo could not reach either. **Ctrl+Enter** now flips it either way, for a whole
selection, and it is undoable.

**No file name under the thumbnail.** Every scan in a project comes from one
folder and is named after it, so four hundred repetitions of that tell you
nothing. The line under a page is reserved for what has been *done* to it, and a
page nobody has touched says nothing at all — which is what makes the ones that
do say something easy to find.

---

## Blurring

**Pixelate at strength 100, by default.** A blur is placed to make something
unreadable — a previous owner's name, a price sticker, a serial number — and a
gentle blur of small text is the failure case: it looks redacted and is not.
Pixelate at that strength destroys the glyphs rather than softening them, so
the default is the one you would have picked anyway on the occasion it matters.

**The strength no longer comes from the size of the rectangle.** It used to be
`min(width, height) * 0.18`, which made a small blur over a serial number
weaker than a large one over a name — exactly backwards, since the small text
is the legible part. It is a setting now.

**The controls are at the blur, not in the side panel.** The only question a
blur raises is whether it is actually illegible, which is a judgement about the
pixels. So a dialog opens next to the page, and the page redraws behind it as
the slider moves.

It appears the first time you place a blur in a session and then stays out of
the way, because a document is redacted the same way throughout and being asked
on every blur would be worse than a panel. Double-clicking a blur brings it
back for that one — which is also why the dialog says so the first time it
opens: a setting you can only reach by a gesture nobody mentioned is a setting
nobody finds.

---

## Matching crop sizes

Scans of one document never come out to the same number of pixels: the sheet
sits a hair differently on the glass every time, and a hand-drawn crop lands a
few pixels off. Eleven pages at 4960×7016 and three at 4958×7014 is a difference
you cannot see and a PDF reader certainly can.

Every crop in a group is resized **about its own centre**, so the sizes change
and the framing does not move.

The changes are small by nature — that is the point of the tool — so the status
bar says how many pages actually moved and by how much, rather than how many
were in the group. "All 14 pages were already 4960×7016" and "12 of 14 pages
resized — the largest change was 2 px" are different outcomes, and both used to
report the same thing, which made a tool that was working look like one that was
not.

The pages that moved also drop their cached thumbnails, so the grid redraws them
instead of showing the old framing until you happen to scroll.

---

## Facing pages

The aligner opens **in the main working area**, in place of the editing canvas,
with the queue and the panel still beside it. It used to be a floating window,
which put the alignment somewhere other than where the work happens.

While it is open, **every page but the two being aligned is greyed out and
cannot be selected**, and the panel is disabled. Both were live before, and both
were traps: clicking a third page in the queue loaded it into the inspector
while the aligner went on editing the original pair, so the panel on the right
described a page that was not on screen and any adjustment made there landed on
the wrong one.

The queue also keeps its width now. The aligner's control strip used to be a
row of group boxes whose combined minimum width set the whole pane's minimum,
which the splitter then took out of the page grid — squeezing it to a single
column, and leaving it there after the aligner closed, saved into the next
session. The divider positions are now restored on the way out, and the strip
itself asks for very little.

**The strip wraps rather than scrolls.** Putting it in a scroll area fixed the
width problem and created a worse one: in any window narrower than the whole
strip, the controls at the right-hand end — Revert, Keep, Join, the
three ways out of the aligner — were off the edge behind a scrollbar. A control
you cannot see is one you do not know exists, and these were the ones that
finish the job.

It is now a flow layout: clusters of related controls wrap onto another line
when they run out of room, so everything is on screen at every width, and the
strip's minimum width is its widest single cluster rather than the sum of all
of them — which is what kept the page grid safe in the first place. The group
boxes went with it. Four titled frames cost a line of text and eight edges of
padding to label controls whose own words already said what they did, and that
padding was most of the reason the strip did not fit. What replaced them is
icons where the app already uses that icon for the same job elsewhere — the
guide buttons are the top bar's guide buttons — and short words where it does
not.

**Link is size only.** Each page keeps its own position over its own scan,
because a spread is made consistent by making the two pages the same shape, not
by dragging one onto the other. Switching the link on matches them straight away
from the active page, rather than waiting for the next drag and leaving the
spread mismatched while claiming to be joined up.

This is what a bound spread usually needs: the two halves were scanned
separately, the sheet sat differently on the glass each time, and the finished
pages still have to be the same size or the book reads as if it were assembled
from offcuts. The frames touch at the gutter, so their handles overlap there —
the active page gets first claim on its own edge.

Spread-aligned pages are **locked** afterwards. Their framing was set against
their facing page, and a crop-all must not come along later and replace it.

**One tool, two endings.** There used to be *Align as spread* and *Join pages
into one*, two buttons that opened the identical panel and differed only in
which exit button was the default. Lining up a spread and lining up two passes
of one sheet are the same work — nudge, rotate, match, guide — so it is one
tool now, and the choice between **Keep as two pages** and **Join into one
page** is made at the end, when you can see the result. Keep is the default
Enter reaches, being the one that changes nothing on disk or in the page count.

### Joined pages are drawn from every scan in them

A joined page's `path` is only its first scan — there is no single file behind
it. Everything that previews a page used to read `path` anyway: the aligner was
handed one scan for a two-scan page, which is why joining a joined page thought
it was the size of a single page, and the book view and the page grid showed the
first scan stretched across the width of both. The canvas had its own assembly
code, one level deep, so a join *of* a join showed its inner half the same
wrong way.

All of it now goes through `render.base_preview`, which assembles a joined page
from its parts' previews and recurses into parts that are joined themselves,
and `PageEdit.source_paths()`, which lists every scan underneath however deep.
The export was never affected — it assembles at full resolution and always
recursed.

**The aligner draws both pages at one scale.** It lays the two pages out in
preview pixels, which only means the same thing on both sides if both previews
are the same fraction of their real size. A joined page broke that: its preview
is held under the same 2200 px cap as a single scan, so a page twice as wide
arrived at two-thirds the scale — and was drawn *shorter* than the single page
it was about to be joined to. The small test scans never reached the cap, so
every preview sat at scale 1.0 and the mismatch could not show; the tests now
use scans big enough to be scaled. The aligner brings the sharper preview down
to the other's scale before laying them out, and a joined page headed for the
aligner is built at its scans' own resolution rather than squeezed, so in
practice nothing needs bringing down. The page grid also caches a joined page's picture under the page
rather than under `path`, which would otherwise have handed the joined picture
back to its first scan after a split.

---

## The book view

What it draws is the *rendered* page — orientation, straightening, crop and
blurs all applied, exactly what the exporter will write — so it is a check on
the product, not on the source.

**The book is fitted once, not per spread.** The scale comes from the largest
pair in the document: the widest left page beside the widest right one, and the
tallest page. Every spread is drawn at the same size, so the book doesn't
breathe as you turn it, nothing overflows whatever the window shape, and a
fold-out that is twice the width of its neighbours simply sets the scale for
everything.

**The turn is animated as one sheet** — its front closes onto the spine, then
its back opens out the other side, 420 ms end to end. Jumping more than one
spread (the slider, Home, End) cuts straight there instead, because an animation
between pages that aren't adjacent is a lie about the document.

**The covers stand where a book puts them.** Page one sits on the *right* of the
spine with its left edge against the gutter — the whole block is still under it,
so it has nowhere else to be. The back cover has nothing left beneath it, so the
book eases sideways until that page is in the middle of the window. It travels
there over the length of the turn rather than jumping, which is what tells you
you have reached the end.

Clicking a page turns it, split at the spine, so the half you clicked is the
half you were reading.

---

## Export

**The individual page files are not kept by default.** The project file already
records every crop, angle and blur against the untouched scans, so the cropped
pages are usually a step on the way to the zip or the PDF rather than the thing
you wanted — and a set of 600 dpi TIFFs is several gigabytes to leave lying
about for nothing to read.

The zip and the PDF are both assembled from files on disk, so this cannot mean
"don't write them". The pages are written to a temporary folder **inside the
destination** — same filesystem, so nothing is copied across devices — bundled,
and then taken away. The export still reports every page it wrote, and the run
reads identically either way. With neither the zip nor the PDF ticked the option
is greyed out and the pages are kept regardless, because otherwise the export
would produce nothing at all.

Defaults are archival: **no TIFF compression**, and a PDF at whatever resolution
the scans themselves are. A 600 dpi PDF of a long manual gets very large, so
drop that number if you want something to email.

**Lanczos** is the default resampler and is sharpest; **Bicubic** is roughly 3×
faster and very hard to tell apart on a 600 dpi scan of print, which makes it
reasonable for a large batch. Pages over 30 MP are written tiled so other
software opens them quickly.

---

## Dialogs

**The export dialog is two columns.** It was one column six groups tall —
970 px, more than a laptop screen, so the Export button was off the bottom.
Left is what you are making and where it goes, with the file preview; right is
how the page files are written, which is set once and left. Nothing was
removed. The resolution box shows only the field that means something for the
chosen mode.

**F1 is a sheet, and it is checked.** It was a message box of plain text that
had drifted from the app: it still listed D for an auto-deskew that had been
removed, and gave Delete for two things without saying which was which. It is
a structured list now, and a test checks it both ways — every shortcut the
window has is on it, and nothing on it is a key the window no longer has.

## The build and its self-test

A frozen build fails in a specific way: everything imports perfectly from
source, then the .exe runs and some C extension PyInstaller never noticed is
missing. Nothing catches that except running the real binary, so `build-exe.bat`
does exactly that and refuses to call the build good until it passes.

It has caught real ones: the TIFF codecs, and Pillow's JPEG encoder — the latter
of which would have failed only when someone got as far as exporting a PDF. It
also caught fourteen methods deleted by an over-broad edit, which every
source-level import passed because nothing called them at import time.

Two things in `scanforge.spec` matter. **imagecodecs is a pile of separate C
extensions**, one per compression format, and PyInstaller only sees the ones
that happen to be imported at analysis time — miss them and the app runs fine
until someone picks LZW in the export dialog. And **PySide6 ships an enormous
amount the app never touches** (a whole web engine, a QML runtime, 3D);
excluding it is the difference between a ~150 MB folder and a ~500 MB one.
`collect_submodules("scanforge")` pulls in the whole app, so a new lazily
imported dialog cannot be left out by accident.

The self-test writes its report to a file as well as printing it. A windowed
PyInstaller build has no stdout — `print` goes nowhere — which made the one
moment the report mattered most the moment it was invisible; the script used to
say "the failing check is listed above" with nothing above it.

`build-exe.bat` deletes `dist\` before building. Without that, a failed build
leaves the last good exe in place and the test and zip steps happily use *that*,
which is how a stale build gets shipped.

UPX compression is deliberately off. Packed Qt DLLs make antivirus far more
suspicious than the size saving is worth.
