"""Frozen, evidence-backed public-data calibration plans (never train on GET)."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

from ..core.config import settings
from . import rules, templates


def load_measured_baselines() -> list[dict[str, Any]]:
    path = settings.project_root / 'configs' / 'measured_baselines.json'
    if not path.is_file():
        return []
    catalog = json.loads(path.read_text(encoding='utf-8-sig'))
    items = catalog.get('baselines')
    if not isinstance(items, list):
        raise ValueError('实测基线目录格式错误')
    seen = set()
    for item in items:
        type_ = item.get('type')
        key = (type_, item.get('modelId'))
        if type_ not in templates.TEMPLATES or key in seen:
            raise ValueError('实测默认基线的模态/模型重复或无效')
        seen.add(key)
        if item.get('modelId') not in {m['id'] for m in templates.MODEL_PROFILES[type_]}:
            raise ValueError('实测默认基线模型未登记')
        evidence = item.get('evidence') or {}
        if (item.get('mode') != 'measured' or item.get('source') != 'builtin'
                or not item.get('isDefault') or evidence.get('sampleCount', 0) <= 0
                or len(evidence.get('runSha256', '')) != 64):
            raise ValueError('实测基线缺少测量证据')
        for metric in evidence.get('metrics') or []:
            value = metric.get('value')
            if value is None:
                if metric.get('status') != 'missing' or not metric.get('reason'):
                    raise ValueError('缺失指标必须明确原因')
            elif not rules.is_num(value):
                raise ValueError('基线指标必须为有限数值')
        rules.validate_config(type_, item['config'])
    return copy.deepcopy(items)
