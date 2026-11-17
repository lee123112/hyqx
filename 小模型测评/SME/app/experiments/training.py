"""Shared dataset-level training adapter for operator and business tasks.

Dataset comparisons retain their own metrics and complete protocol record;
they never manufacture per-input measurements to fit the legacy proxy schema.
"""
from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
from .training_lock import TRAINING_LOCK as _TRAINING_LOCK
from typing import Any, get_type_hints

from pydantic import TypeAdapter, ValidationError

from ..core.config import settings
from ..core.envelope import ApiError
from ..eval import rules




def allowed_roots() -> list[Path]:
    return [p.resolve() for p in (settings.project_root.parent, settings.dataset_dir,
                                 settings.data_dir, settings.workspace_dir)]


def _path(raw: Any, base: Path, directory: bool) -> Path:
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise ApiError(422, "REQUEST_INVALID", "训练协议的路径不能为空")
    path = Path(raw).expanduser()
    path = (path if path.is_absolute() else base / path).resolve()
    if not any(path.is_relative_to(root) for root in allowed_roots()):
        raise ApiError(422, "REQUEST_INVALID", "训练协议路径超出允许的数据目录")
    if not (path.is_dir() if directory else path.is_file()):
        raise ApiError(422, "ASSET_MISSING", f"训练协议资产不存在：{path.name}")
    return path


def _camel(name: str) -> str:
    first, *rest = name.split('_')
    return first + ''.join(part.title() for part in rest)


def build_training_spec(type_: str, payload: dict[str, Any], *,
                        base_dir: Path | None = None) -> Any:
    if not isinstance(payload, dict):
        raise ApiError(422, "REQUEST_INVALID", "protocol 必须是训练协议对象")
    if type_ == 'image':
        from .image_protocol import ImageProtocolSpec, TORCHVISION_MODELS
        cls = ImageProtocolSpec
        path_fields = {'ours_dir': True, 'ref_dir': True, 'eval_dir': True, 'model_weight': False}
    elif type_ == 'text':
        from .text_downstream import TextProtocolSpec
        cls = TextProtocolSpec
        path_fields = {name: False for name in ('ours_corpus','ref_corpus','train_file','eval_file')}
        path_fields.update(model_path=True, tokenizer_path=True)
    elif type_ == 'audio':
        from .audio_protocol import AudioProtocolSpec
        cls = AudioProtocolSpec
        path_fields = {name: False for name in ('ours_manifest','ref_manifest','eval_manifest')}
        path_fields['model_dir'] = True
    else:
        raise ApiError(422, 'CONFIG_INVALID', '训练协议仅支持 text、image、audio')
    aliases = {alias: f.name for f in fields(cls) for alias in (f.name, _camel(f.name))}
    values = {}
    for key, value in payload.items():
        if key not in aliases:
            raise ApiError(422, 'CONFIG_INVALID', f'未知训练协议字段：{key}')
        field = aliases[key]
        if field in values:
            raise ApiError(422, 'CONFIG_INVALID', f'训练协议字段重复：{field}')
        values[field] = value
    base = (base_dir or settings.project_root.parent).resolve()
    for name, directory in path_fields.items():
        if name in values and values[name] is not None:
            values[name] = _path(values[name], base, directory)
    from ..core.model_assets import bundled_model_path, image_weight
    if type_ == 'text' and not values.get('model_path'):
        values['model_path'] = bundled_model_path('text')
    elif type_ == 'audio' and not values.get('model_dir'):
        values['model_dir'] = bundled_model_path('audio')
    elif type_ == 'image' and 'model_weight' not in values:
        values['model_weight'] = image_weight(values.get('proxy_model', 'resnet18'), required=True)
    if values.get('seeds') is not None:
        if not isinstance(values['seeds'], (list, tuple)):
            raise ApiError(422, 'CONFIG_INVALID', 'seeds 必须为整数数组')
        values['seeds'] = tuple(values['seeds'])
    try:
        hints = get_type_hints(cls)
        typed = {key: TypeAdapter(hints[key]).validate_python(value, strict=True)
                 for key, value in values.items()}
        return cls(**typed)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ApiError(422, 'CONFIG_INVALID', '训练协议字段类型、必填项或参数范围无效') from exc


def run_training_protocol(type_: str, payload: dict[str, Any], *,
                          base_dir: Path | None = None) -> dict[str, Any]:
    spec = build_training_spec(type_, payload, base_dir=base_dir)
    with _TRAINING_LOCK:
        if type_ == 'image':
            from .image_protocol import run_image_protocol
            return run_image_protocol(spec)
        if type_ == 'text':
            from .text_downstream import run_text_experiment
            return run_text_experiment(spec)
        from .audio_protocol import run_audio_protocol
        return run_audio_protocol(spec)


def _mean(value: Any) -> float:
    return float(value['mean'] if isinstance(value, dict) else value)


def training_evaluation(type_: str, record: dict[str, Any]) -> dict[str, Any]:
    """Adapt a dataset result without declaring an unspecified business gate."""
    protocol = record.get('protocol') or {}
    eligible = bool(record.get('qualityConclusionEligible', record.get('conclusionEligible', False)))
    metrics = []
    for key, ours in record['S_ours'].items():
        baseline, candidate = _mean(record['S_ref'][key]), _mean(ours)
        metrics.append({
            'key': key, 'name': key, 'unit': '%' if key != 'lossCv' else '',
            'baseline': baseline, 'candidate': candidate,
            'delta': round(candidate - baseline, 6),
            'direction': 'lower' if key == 'cer' else 'higher',
            'threshold': None, 'passed': None,
        })
    total = (protocol.get('evalVolume') or (protocol.get('downstream') or {}).get('evalSize')
             or ((record.get('data') or {}).get('eval') or {}).get('count') or 0)
    summary = {
        'scope': 'dataset', 'total': total, 'passed': 0, 'issues': 0,
        'grades': {'AA': 0, 'A': 0, 'B': 0, 'C': 0}, 'metrics': metrics,
        'accepted': None, 'qualityConclusionEligible': eligible,
        'conclusion': ('协议比较有效，待业务判定' if eligible else '探索结果，证据不足以作数据质量结论'),
    }
    audit = {'provenance': record.get('provenance') or {},
             'meta': {'engineVersion': record.get('engineVersion') or record.get('version')
                      or record.get('experiment'), 'metricNature': 'dataset_training_comparison',
                      'protocol': protocol, 'qualityConclusionEligible': eligible},
             'missing': [], 'notes': [*(record.get('notes') or record.get('limitations') or []),
                '数据集级比较不生成虚构逐条指标；业务接受阈值未配置，accepted=null。']}
    return {'samples': [], 'summary': summary, 'engine': audit,
            'provenance': audit['provenance'], 'trainingProtocol': record}


def evaluate_measured(type_: str, samples: list[dict[str, Any]], config: dict[str, Any],
                      audit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Only held-out text rows enter quality aggregates; retain train diagnostics."""
    meta = (audit or {}).get('meta') or {}
    evaluation = meta.get('evaluation') or {}
    selected = samples
    training_rows = []
    if evaluation.get('policy') == 'heldout':
        ids = set(evaluation.get('sampleIds') or [])
        if not ids:
            raise ApiError(422, 'EVALUATION_INVALID', '没有独立验证样本，无法生成评测汇总')
        selected = [row for row in samples if str(row['id']) in ids]
        if len(selected) != len(ids):
            raise ApiError(422, 'EVALUATION_INVALID', '独立验证ID与测量样本不一致')
        training_rows = [row for row in samples if str(row['id']) not in ids]
    result = rules.evaluate(type_, selected, config)
    if training_rows:
        result['diagnostics'] = {'trainingSamples': training_rows,
                                 'note': '训练内诊断不计入评测汇总或逐条反馈'}
    if audit:
        result['engine'] = audit
        result['provenance'] = audit.get('provenance') or {}
        if evaluation or any(value == 'proxy' for value in result['provenance'].values()):
            result['summary']['qualityConclusionEligible'] = False
    return result


def protocol_markdown(result: dict[str, Any], *, title: str = '训练式数据集评测') -> str:
    record, summary = result['trainingProtocol'], result['summary']
    lines = [f'# {title}', '', f"- 结论：{summary['conclusion']}",
             f"- 质量比较资格：{summary['qualityConclusionEligible']}",
             '- 业务接受状态：待判定（未配置业务阈值，accepted=null）',
             '- 评测范围：独立固定 E 的数据集级指标；不生成逐条质量标签。',
             '', '## 协议', '', '```json',
             json.dumps(record.get('protocol') or {}, ensure_ascii=False, indent=2),
             '```', '', '## 指标', '',
             '| 指标 | S_ref | S_ours | ours−ref | 方向 |',
             '|---|---:|---:|---:|---|']
    for metric in summary['metrics']:
        lines.append(f"| {metric['key']} | {metric['baseline']} | {metric['candidate']} | "
                     f"{metric['delta']} | {'越低越好' if metric['direction']=='lower' else '越高越好'} |")
    statistics = record.get('statistics')
    if statistics:
        lines += ['', '## 多种子统计', '', '```json',
                  json.dumps(statistics, ensure_ascii=False, indent=2), '```']
    elif record.get('type') == 'audio-quality-protocol':
        lines += ['', '## 多种子统计', '', '```json',
                  json.dumps({k:record[k] for k in ('S_ours','S_ref','delta')}, ensure_ascii=False, indent=2), '```']
    lines += ['', '## 解释边界', '']
    lines += ['- ' + str(note) for note in (result['engine']['notes'])]
    return '\n'.join(lines) + '\n'
