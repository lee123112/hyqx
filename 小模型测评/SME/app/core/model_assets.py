"""Resolve deployable model assets owned by each operator, never shared weight/."""
from __future__ import annotations

from pathlib import Path

from .config import settings
from .envelope import ApiError

PACKAGES = {
    'text': 'SME_EVAL_TEXT_001', 'image': 'SME_EVAL_IMAGE_001',
    'audio': 'SME_EVAL_AUDIO_001', 'video': 'SME_EVAL_VIDEO_001',
    'benchmark': 'SME_EVAL_BENCHMARK_001',
}
IMAGE_WEIGHTS = {
    'resnet18': 'resnet18/resnet18-f37072fd.pth',
    'resnet50': 'resnet50/resnet50-0676ba61.pth',
    'mobilenet_v2': 'mobilenet_v2/mobilenet_v2-b0353104.pth',
}
DEFAULTS = {'text': 'bert-base-chinese', 'audio': 'whisper-base',
            'image': IMAGE_WEIGHTS['resnet18'], 'video': 'clip/ViT-L-14.pt'}


def operator_models_root(type_: str) -> Path:
    if type_ not in PACKAGES:
        raise ApiError(422, 'CONFIG_INVALID', '未知算子模型类型')
    return (settings.project_root / PACKAGES[type_] / 'models').resolve()


def bundled_model_path(type_: str, name: str | None = None, *, required: bool = True) -> Path:
    root = operator_models_root(type_)
    raw = name or DEFAULTS.get(type_)
    if not raw:
        raise ApiError(422, 'CONFIG_INVALID', '此算子不需要模型资产')
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ApiError(422, 'CONFIG_INVALID', '模型名称不得越出所属算子models目录')
    if required and not path.exists():
        raise ApiError(422, 'MODEL_ASSET_MISSING',
                       f'算子内模型缺失：{PACKAGES[type_]}/models/{raw}')
    return path


def image_weight(model: str, *, required: bool = False) -> Path | None:
    if not isinstance(model, str):
        raise ApiError(422, 'CONFIG_INVALID', 'proxyModel必须为模型名称字符串')
    name = IMAGE_WEIGHTS.get(model)
    if name is None:
        return None
    path = bundled_model_path('image', name, required=required)
    return path if path.is_file() else None
