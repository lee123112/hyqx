"""规范日志：一行一个动作，格式 [YYYY-MM-DD HH:mm:ss] [动作] 内容。

只记录动作、样本标识和真实数量；不记录密码、Token、Cookie、完整原文或大段 JSON。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional


def format_line(action: str, message: str) -> str:
    return "[%s] [%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, message)


class Logger:
    """同时写标准输出与 run.log 的轻量日志器。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, action: str, message: str) -> None:
        line = format_line(action, message)
        print(line, flush=True)
        if self._path is not None:
            with open(str(self._path), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def log(action: str, message: str) -> None:
    Logger()(action, message)
