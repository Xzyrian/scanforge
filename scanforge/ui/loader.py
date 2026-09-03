"""Asynchronous page loading.

Nothing that decodes an image is allowed to run on the GUI thread.  The first
version loaded proxies inline and pumped the event loop while the detail tier
decoded, which is exactly the intermittent freeze you feel when stepping
between scans: a page whose proxy was not cached yet meant a full 140 MP TIFF
decode with the interface wedged behind it.

Here every decode happens on a worker thread and comes back as a signal.  The
window shows the page the instant its proxy is ready and stays responsive in
the meantime.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal

from .. import proxycache


class AsyncLoader(QObject):
    """Decodes proxy tiers off-thread and hands the arrays back on the GUI thread."""

    ready = Signal(str, str, object)      # path, tier, ndarray or None
    failed = Signal(str, str)             # path, message

    def __init__(self, workers: int = 3, parent=None):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers),
                                        thread_name_prefix="page")
        self._inflight: set = set()
        self._lock = threading.Lock()
        self._closed = False

    def request(self, path: str, tier: str = "proxy") -> bool:
        """Queue a decode.  Returns False if this exact job is already running."""
        if self._closed:
            return False
        key = (path, tier)
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)
        self._pool.submit(self._work, path, tier)
        return True

    def _work(self, path: str, tier: str) -> None:
        arr: Optional[np.ndarray] = None
        error = ""
        try:
            arr = proxycache.load(path, tier)
            if arr is None:
                error = "could not decode"
        except Exception as exc:                       # noqa: BLE001
            error = str(exc)
        finally:
            with self._lock:
                self._inflight.discard((path, tier))
        if self._closed:
            return
        if error:
            self.failed.emit(path, error)
        else:
            self.ready.emit(path, tier, arr)

    def busy(self) -> int:
        with self._lock:
            return len(self._inflight)

    def shutdown(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


class Background(QObject):
    """Runs one-off work off the GUI thread and reports back by signal.

    Anything that might take more than a frame belongs here.  The alternative -
    doing it inline and calling QApplication.processEvents() to keep the window
    painting - is what used to wedge the app: processEvents dispatches queued
    clicks *inside* the handler that is still running, so a second action
    starts on top of the first and the two trample each other.
    """

    done = Signal(str, object)        # token, result
    failed = Signal(str, str)         # token, message

    def __init__(self, workers: int = 2, parent=None):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers),
                                        thread_name_prefix="bg")
        self._closed = False

    def run(self, token: str, fn, *args, **kwargs) -> None:
        if self._closed:
            return
        self._pool.submit(self._work, token, fn, args, kwargs)

    def _work(self, token: str, fn, args, kwargs) -> None:
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:                       # noqa: BLE001
            if not self._closed:
                self.failed.emit(token, str(exc))
            return
        if not self._closed:
            self.done.emit(token, result)

    def shutdown(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
