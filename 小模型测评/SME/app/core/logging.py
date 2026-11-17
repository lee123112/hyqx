"""Structured run logs following the platform log template."""
from __future__ import annotations

from pathlib import Path

from .config import settings
from .envelope import ApiError
from .ids import now
from .security import sanitize_text

ALLOWED_TAGS = {
    "开始", "路由", "输入", "校验", "读取", "处理", "输出", "上传",
    "警告", "错误", "重试", "完成", "等待",
}


class RunLogger:
    def __init__(self, path: Path | None = None, unique_id: str = "") -> None:
        self.path = path
        self.unique_id = unique_id
        self.entries: list[dict[str, str]] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def log(self, tag: str, message: str) -> dict[str, str]:
        tag = tag if tag in ALLOWED_TAGS else "处理"
        message = sanitize_text(message, [settings.workspace_dir])[: settings.log_max_chars]
        entry = {"time": now(), "tag": tag, "message": message}
        self.entries.append(entry)
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{entry['time']}] [{tag}] {message}\n")
        return entry

    def fail(self, message: str, code: str = "INTERNAL_ERROR") -> ApiError:
        self.log("错误", message)
        return ApiError(500, code, message)
