from __future__ import annotations

import threading


class ResourceHotReloader:
    def __init__(self, manager, *, poll_interval_s: float):
        self._manager = manager
        self._poll_interval_s = max(0.2, float(poll_interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="g3ku-resource-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval_s):
            try:
                self._manager.reload_now(trigger="watcher")
            except Exception:
                pass
