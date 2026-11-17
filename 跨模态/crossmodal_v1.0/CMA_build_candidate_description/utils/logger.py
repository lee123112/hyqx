"""规范日志：每行一个动作，格式 [时间] [动作] 内容；不输出原文、密钥或大段 JSON。"""
from __future__ import annotations

from datetime import datetime


def log(action: str, message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{action}] {message}", flush=True)
