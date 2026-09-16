from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType
from typing import IO


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.file_handle: IO[str] | None = None

    def __enter__(self) -> "ProcessLock":
        self.file_handle = self.path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file_handle.close()
            self.file_handle = None
            raise RuntimeError("已有同步任务正在运行") from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.file_handle is not None:
            fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
            self.file_handle.close()
            self.file_handle = None
