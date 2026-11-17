# -*- coding: utf-8 -*-
"""按《统一算子前端日志显示规范与模板 v1.1》产出日志行。

前端「处理过程」日志列表直接渲染 :class:`RunLog` 的行，因此必须遵守模板：

- 一行只记一个动作/结果，按发生顺序。
- 每行 ``[YYYY-MM-DD HH:mm:ss] [阶段标签] 简短消息（可附统计）``。
- 中文动作优先：开始 / 加载 / 读取 / 处理 / 写入 / 上传 / 完成 / 警告 / 失败。
- 单行 ≤ 160 字符；文件名只留 basename；不出现容器绝对路径、哈希、堆栈、大段 JSON。
- 正常完成 / 部分完成 / 失败 / 等待人工 必须用不同措辞，不能都写「执行成功」。
- 统计只写用户判断进度需要的量，未知就不写，不用 0 冒充。

后台追踪用的异常堆栈请在调用方 ``logger.exception``，不要塞进这里。
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import List, Optional, Sequence

MAX_LINE_LEN = 160

STAGE_TAGS: Sequence[str] = (
    "开始",
    "路由",
    "输入",
    "校验",
    "读取",
    "处理",
    "输出",
    "上传",
    "警告",
    "错误",
    "重试",
    "等待",
    "完成",
    "部分完成",
)


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _basename(value) -> str:
    try:
        return os.path.basename(str(value))
    except Exception:  # noqa: BLE001
        return str(value)


def fmt(stage: Optional[str], message: str, stats: Optional[str] = None) -> str:
    """拼一行日志（自动限长，不截断到半个括号）。"""
    msg = str(message).strip().replace("\r", " ").replace("\n", " ")
    if stats:
        msg = "%s：%s" % (msg.rstrip("：:"), str(stats).strip())
    head = "[%s]" % stage if stage else ""
    line = "%s %s %s" % ("[%s]" % _now(), head, msg)
    line = " ".join(line.split())
    if len(line) > MAX_LINE_LEN:
        line = line[: MAX_LINE_LEN - 1] + "…"
    return line


class RunLog:
    """一次算子运行的日志收集器。"""

    def __init__(self, operator_name: str, clock: Optional[callable] = None):
        self.operator_name = operator_name
        self._clock = clock or _now
        self._lines: List[str] = []
        self._started = _dt.datetime.now()
        self._start_logged = False
        self._done_logged = False

    # -- 基础 -------------------------------------------------------------- #
    def add(self, stage: Optional[str], message: str, stats: Optional[str] = None) -> str:
        line = fmt(stage, message, stats)
        if self._clock is not _now:
            # 测试注入时钟：替换时间戳前缀
            line = "[%s] %s" % (self._clock(), line.split("] ", 1)[1])
        self._lines.append(line)
        return line

    def lines(self) -> List[str]:
        return list(self._lines)

    def __len__(self) -> int:  # pragma: no cover - 便捷
        return len(self._lines)

    def elapsed_sec(self) -> float:
        return round((_dt.datetime.now() - self._started).total_seconds(), 2)

    # -- 标准模板（4.1 ~ 4.16） -------------------------------------------- #
    def start(self) -> str:
        """「开始」行。**幂等**：服务层与 processor 都可调用，只出一次。"""
        if self._start_logged:
            return self._lines[0] if self._lines else ""
        self._start_logged = True
        return self.add(None, "开始执行算子：%s" % self.operator_name)

    def route(self, target: str) -> str:
        return self.add("路由", "%s → %s" % (self.operator_name, target))

    def load_input(self, filename) -> str:
        return self.add("输入", "加载输入文件：%s" % _basename(filename))

    def validated(self, filename) -> str:
        return self.add("校验", "输入文件校验通过：%s" % _basename(filename))

    def read(self, count: int, unit: str = "条记录") -> str:
        return self.add("读取", "读取 %d %s" % (int(count), unit))

    def progress(self, done: int, total: int) -> str:
        pct = 0.0 if not total else 100.0 * float(done) / float(total)
        return self.add("处理", "正在处理", "已完成 %d/%d（%.1f%%）" % (done, total, pct))

    def result(
        self,
        action: str,
        output: int,
        success: Optional[int] = None,
        skipped: Optional[int] = None,
        failed: Optional[int] = None,
        extra: Optional[dict] = None,
    ) -> str:
        bits = ["输出 %d 条" % int(output)]
        if success is not None:
            bits.append("成功 %d 条" % int(success))
        if skipped is not None:
            bits.append("跳过 %d 条" % int(skipped))
        if failed is not None:
            bits.append("失败 %d 条" % int(failed))
        for key, val in (extra or {}).items():
            bits.append("%s %s" % (key, val))
        return self.add("处理", "%s完成" % action, "，".join(bits))

    def write_output(self, filename) -> str:
        return self.add("输出", "写入输出文件：%s" % _basename(filename))

    def upload(self, filename) -> str:
        return self.add("上传", "产物已上传：%s" % _basename(filename))

    def warning(self, message: str, stats: Optional[str] = None) -> str:
        """警告行。``stats`` 用于附统计（如「短音频 3、空音频 1」）。"""
        return self.add("警告", message, stats)

    def error(self, action: str, reason: str) -> str:
        return self.add("错误", "%s失败：%s" % (action, reason))

    def retry(self, current: int, maximum: int) -> str:
        return self.add("重试", "第 %d/%d 次重试" % (int(current), int(maximum)))

    def waiting(self, item: str) -> str:
        return self.add("等待", "%s，请完成确认" % item)

    def done(self) -> str:
        """「完成」行。**幂等**：只出一次，避免服务层与 processor 重复写。"""
        if self._done_logged:
            return ""
        self._done_logged = True
        return self.add("完成", "%s执行完成，耗时 %.2f 秒" % (self.operator_name, self.elapsed_sec()))

    def partial(self, message: str, stats: Optional[str] = None) -> str:
        return self.add("部分完成", message, stats)
