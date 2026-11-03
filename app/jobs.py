"""One background job at a time (download + backtest), with progress."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from datetime import datetime


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict = {"status": "idle"}

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def progress(self, stage: str, done: int, total: int, message: str) -> None:
        self.state.update(stage=stage, done=done, total=total, message=message)

    def start(self, name: str, fn: Callable[[Callable], object]) -> bool:
        """Run ``fn(progress)`` in a thread. False if a job is already running."""
        with self._lock:
            if self.busy:
                return False
            self.state = {
                "status": "running",
                "name": name,
                "stage": "starting",
                "done": 0,
                "total": 0,
                "message": "Starting...",
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }

            def target() -> None:
                try:
                    result = fn(self.progress)
                    self.state.update(status="done", result=result, message="Finished")
                except Exception as exc:
                    self.state.update(
                        status="error",
                        message=f"{type(exc).__name__}: {exc}",
                        trace=traceback.format_exc(limit=3),
                    )
                finally:
                    self.state["finished_at"] = datetime.now().isoformat(
                        timespec="seconds"
                    )

            self._thread = threading.Thread(
                target=target, name=f"job-{name}", daemon=True
            )
            self._thread.start()
            return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)
