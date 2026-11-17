"""规范日志封装：每行一条，格式 [时间] [动作] 消息。

安全约束：调用方只允许传入类型名与数量，禁止传入原文、敏感值、映射表或 token。
"""
import logging
from datetime import datetime, timezone, timedelta

_TZ = timezone(timedelta(hours=8))


def log_action(logger: logging.Logger, action: str, message: str) -> None:
    ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"[{ts}] [{action}] {message}")


def get_logger(name: str, log_file=None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter("%(message)s")
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        if log_file:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
    return logger


def close_logger(logger: logging.Logger) -> None:
    """关闭并移除所有 handler，释放文件句柄（Windows 下不释放会导致任务目录无法删除）。"""
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)
