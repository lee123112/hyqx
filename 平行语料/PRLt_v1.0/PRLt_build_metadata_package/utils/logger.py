"""规范日志封装：每行一条，格式 [时间] [动作] 消息。"""
import logging
import os
from datetime import datetime, timedelta, timezone

_TZ = timezone(timedelta(hours=8))


def log_action(logger: logging.Logger, action: str, message: str) -> None:
    ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("[%s] [%s] %s" % (ts, action, message))


def _is_console_handler(handler: logging.Handler) -> bool:
    # FileHandler 是 StreamHandler 的子类，必须排除，否则会把它当成控制台输出。
    return type(handler) is logging.StreamHandler


def _same_file(handler: logging.Handler, path: str) -> bool:
    if not isinstance(handler, logging.FileHandler):
        return False
    try:
        return os.path.abspath(handler.baseFilename) == path
    except (AttributeError, ValueError):
        return False


def get_logger(name: str, log_file=None) -> logging.Logger:
    """获取任务级 logger，并确保 run.log 的 FileHandler 已挂到本次任务目录。

    [2026-11-04 修复] 原实现用 `if not logger.handlers` 一次性初始化：只要该
    同名 logger 上已有任意 handler（pytest 的 LogCaptureHandler、宿主平台自带的
    控制台/采集 handler，或上一任务残留的 FileHandler），就再也不会挂本次任务的
    run.log，导致任务目录缺少日志、阶段产物打包缺成员。现在改为逐项检查：
    控制台 handler 只补一次；FileHandler 始终对齐到本次请求的路径，指向旧任务
    目录的句柄先关闭再摘除，避免句柄泄漏（Windows 下还会锁住旧目录）。
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(message)s")

    if not any(_is_console_handler(h) for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh._prlt_owned = True
        logger.addHandler(sh)

    target = os.path.abspath(str(log_file)) if log_file else None
    for handler in list(logger.handlers):
        if getattr(handler, "_prlt_owned", False) and not _same_file(handler, target):
            handler.close()
            logger.removeHandler(handler)

    if target and not any(_same_file(h, target) for h in logger.handlers):
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fh = logging.FileHandler(target, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._prlt_owned = True
        logger.addHandler(fh)
    return logger


def close_logger(logger: logging.Logger) -> None:
    """关闭并移除本算子持有的 handler，释放文件句柄（Windows 下不释放会导致任务目录无法删除）。

    只摘除本模块自己挂上的 handler，保留宿主或测试框架自行挂上的 handler
    （如 pytest 的 LogCaptureHandler），避免影响调用方的日志采集。
    """
    for handler in list(logger.handlers):
        if getattr(handler, "_prlt_owned", False):
            handler.close()
            logger.removeHandler(handler)
