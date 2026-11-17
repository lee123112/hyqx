"""时间戳工具（无血缘改造：原 utils/lineage.now_iso 迁出，时区与格式保持不变）。"""
from datetime import datetime, timedelta, timezone

_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")
