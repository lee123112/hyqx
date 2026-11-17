"""运行日志：每行包含时间、动作与真实数量。

安全约束：不写密码/Token/Cookie，不写完整原文，不写完整堆栈，不写大段 JSON。
所有值都会被截断到 ``MAX_VALUE_CHARS``。
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["RunLogger", "get_logger"]

MAX_VALUE_CHARS = 120


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sanitize(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS] + "…"
    return text


class RunLogger:
    """写入 ``<run_dir>/18_run.log`` 的逐行日志。"""

    def __init__(self, path: Optional[Path], name: str = "operator_18"):
        self.name = name
        self.path = Path(path) if path else None
        self._handle = None
        self.lines = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8", newline="\n")

    def event(self, action: str, message: str = "", **counts: Any) -> str:
        parts = [_timestamp(), self.name, _sanitize(action)]
        if message:
            parts.append(_sanitize(message))
        if counts:
            parts.append(" ".join("%s=%s" % (key, _sanitize(value))
                                  for key, value in sorted(counts.items())))
        line = " | ".join(parts)
        self.lines += 1
        if self._handle is not None:
            self._handle.write(line + "\n")
            self._handle.flush()
        return line

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


_LOGGERS: Dict[str, logging.Logger] = {}


def get_logger(name: str = "operator_18") -> logging.Logger:
    """进程级标准日志器（控制台），供 API/CLI 输出关键动作。"""
    logger = _LOGGERS.get(name)
    if logger is not None:
        return logger
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(name)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("OPERATOR_LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    _LOGGERS[name] = logger
    return logger
