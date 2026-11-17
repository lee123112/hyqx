"""运行日志：逐行「时间 + 动作 + 真实数量」，且自动屏蔽敏感与超长内容。

禁止写入：密码、Token、Cookie、Authorization、完整原文、完整堆栈、大段 JSON。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

SENSITIVE_KEYS = ("password", "passwd", "secret", "token", "cookie",
                  "authorization", "api_key", "apikey", "access_key")
MAX_VALUE_CHARS = 160
REDACTED = "[redacted]"
TRUNCATED_SUFFIX = "...[truncated]"


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _sanitize(key: str, value: Any) -> str:
    lowered = key.lower()
    if any(marker in lowered for marker in SENSITIVE_KEYS):
        return REDACTED
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return "count=%d" % len(value)
    if isinstance(value, dict):
        return "keys=%d" % len(value)
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s{2,}", " ", text).strip()
    if len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS] + TRUNCATED_SUFFIX
    return text


class TeeStream:
    """同时写多个流（stderr + ``<run_dir>/run.log``）。"""

    def __init__(self, *streams) -> None:
        self._streams = [item for item in streams if item is not None]

    def write(self, text: str) -> int:
        for stream in self._streams:
            try:
                stream.write(text)
            except Exception:  # noqa: BLE001 - 单个 sink 失败不影响业务
                continue
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                continue


class OperatorLogger:
    """单行结构化文本日志；同时写 stderr 与（可选）``<run_dir>/run.log``。

    实现约定：``run.log`` 采用「每次写一行、append 后立即关闭」的方式，
    运行期不长期持有文件句柄。原因是 Windows 下未关闭的句柄会让
    ``/removeFile`` 删除任务目录失败（WinError 32），而异常路径下的
    句柄归属很难穷尽；逐行 append 同时保证日志始终落盘。
    """

    def __init__(self, operator: str, version: str, unique_id: str = "-",
                 run_id: str = "-", stream=None, log_path=None):
        self.prefix = {
            "operator": operator,
            "version": version,
            "uniqueId": unique_id,
            "runId": run_id,
        }
        self.log_path: Optional[Path] = None
        self._closed = False
        sinks = [stream if stream is not None else sys.stderr]
        if log_path is not None:
            target = Path(log_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=True)
            self.log_path = target
        self.stream = TeeStream(*sinks)

    def emit(self, action: str, level: str = "INFO", **fields: Any) -> str:
        payload: Dict[str, Any] = dict(self.prefix)
        payload.update(fields)
        rendered = " ".join(
            "%s=%s" % (key, _sanitize(key, payload[key])) for key in sorted(payload))
        action_text = _sanitize("action", action)
        line = "%s | %-5s | %s | %s" % (_timestamp(), level.upper(), action_text, rendered)
        self.stream.write(line + "\n")
        self.stream.flush()
        self._append_line(line + "\n")
        return line

    def info(self, action: str, **fields: Any) -> str:
        return self.emit(action, "INFO", **fields)

    def warning(self, action: str, **fields: Any) -> str:
        return self.emit(action, "WARN", **fields)

    def error(self, action: str, **fields: Any) -> str:
        return self.emit(action, "ERROR", **fields)

    def _append_line(self, text: str) -> None:
        if self.log_path is None or self._closed:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError:
            return

    def close(self) -> None:
        """标记本次运行日志结束（不持有长句柄，无需释放文件）。"""
        self._closed = True


def dumps_safe(payload: Any, *, max_chars: int = 400) -> str:
    """把结构化对象压缩成可安全写入日志的短串。"""
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(payload)
    if len(text) > max_chars:
        text = text[:max_chars] + TRUNCATED_SUFFIX
    return text
