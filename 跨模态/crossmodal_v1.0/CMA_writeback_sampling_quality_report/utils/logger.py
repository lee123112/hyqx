"""规范日志封装：每行一条动作，格式 ``[时间] [动作] 消息``。

日志约束（背景规则 §8）：

- 每行只记录一个动作或结果，并带真实数量；
- 不输出输入原文、密钥、token、Cookie、完整堆栈或大段 JSON；
- 统计必须来自真实结果。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

_TZ = timezone(timedelta(hours=8))


def log_action(logger: logging.Logger, action: str, message: str) -> None:
    ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("[%s] [%s] %s", ts, action, message)


def get_logger(name: str, log_file=None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        formatter = logging.Formatter("%(message)s")
        # 日志走 stderr：CLI 的 stdout 只保留结构化 JSON
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)
        if log_file is not None:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    """关闭并移除所有 handler（Windows 下不释放句柄会导致任务目录无法删除）。"""
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 释放句柄失败不应影响流程
            pass
        logger.removeHandler(handler)
