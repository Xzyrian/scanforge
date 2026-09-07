"""Undo / redo.

Snapshots are whole-project, which sounds heavy but is not: a PageEdit is a
handful of numbers and two short strings, so a 400-page project snapshots to
well under a megabyte and restoring is instant.  Doing it this way means every
operation - including the batch ones that touch every page at once - is
undoable without any per-operation bookkeeping.

Restores update the existing PageEdit objects in place wherever the uid still
matches, so references held by the window and the canvas stay valid.
"""

from __future__ import annotations

from typing import Callable, Optional

from .model import PageEdit, Project


def snapshot(project: Project) -> dict:
    return {
        "pages": [p.to_dict() for p in project.pages],
    }


def restore(project: Project, state: dict) -> None:
    existing = {p.uid: p for p in project.pages}
    pages = []
    for d in state["pages"]:
        page = existing.get(d.get("uid"))
        if page is None:
            page = PageEdit.from_dict(d)
        else:
            page.__dict__.update(d)
        pages.append(page)
    project.pages = pages


class History:
    def __init__(self, project: Project, limit: int = 250):
        self.project = project
        self.limit = limit
        self._undo: list = []
        self._redo: list = []
        self._pending: Optional[tuple] = None
        self.changed: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------ state
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    def redo_label(self) -> str:
        return self._redo[-1][0] if self._redo else ""

    def _notify(self) -> None:
        if self.changed:
            self.changed()

    # ---------------------------------------------------------------- capture
    def capture(self, label: str) -> None:
        """Record the state as it is *before* the change described by `label`."""
        self._undo.append((label, snapshot(self.project)))
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        self._notify()

    def begin(self, label: str) -> None:
        """Open a coalescing group - a drag, or a spin box being scrubbed.

        Repeated begin() calls with the same label while one is open are
        ignored, so a 200-event drag lands in the history exactly once.
        """
        if self._pending is not None and self._pending[0] == label:
            return
        self._pending = (label, snapshot(self.project))

    def commit(self) -> None:
        """Close a group, discarding it if nothing actually changed."""
        if self._pending is None:
            return
        label, state = self._pending
        self._pending = None
        if state == snapshot(self.project):
            return
        self._undo.append((label, state))
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        self._notify()

    def abandon(self) -> None:
        self._pending = None

    # ------------------------------------------------------------------- move
    def undo(self) -> str:
        if not self._undo:
            return ""
        label, state = self._undo.pop()
        self._redo.append((label, snapshot(self.project)))
        restore(self.project, state)
        self._notify()
        return label

    def redo(self) -> str:
        if not self._redo:
            return ""
        label, state = self._redo.pop()
        self._undo.append((label, snapshot(self.project)))
        restore(self.project, state)
        self._notify()
        return label

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._pending = None
        self._notify()
