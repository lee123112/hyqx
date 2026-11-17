"""Task records for dataset-level protocols, separate from per-row proxies."""
from copy import deepcopy
from typing import Any

from ..core.envelope import ApiError
from ..core.ids import is_valid_id, new_task_id, now
from ..engines.service import validate_seed
from ..store import db
from . import templates


def build_protocol_task(payload: dict[str, Any], *, allow_existing: bool = False) -> dict[str, Any]:
    type_ = payload.get('type')
    if type_ not in {'text','image','audio'} or payload.get('mode', 'measured') != 'measured':
        raise ApiError(422, 'CONFIG_INVALID', '训练协议仅支持 text/image/audio 的 measured 模式')
    protocol = payload.get('protocol')
    if not isinstance(protocol, dict) or not protocol:
        raise ApiError(422, 'CONFIG_INVALID', '训练协议不能为空')
    name = payload.get('name')
    if not isinstance(name, str) or not name.strip():
        raise ApiError(422, 'REQUEST_INVALID', '请输入评测任务名称')
    protocol = deepcopy(protocol)
    seeds = protocol.get('seeds')
    protocol_seed = seeds[0] if isinstance(seeds, (list,tuple)) and seeds else protocol.get('seed')
    seed = validate_seed(payload.get('seed', protocol_seed if protocol_seed is not None else 42))
    if protocol_seed is not None and protocol_seed != seed:
        raise ApiError(422, 'CONFIG_INVALID', '任务 seed 必须与协议首个种子一致')
    if protocol_seed is None and 'seed' in payload:
        protocol['seeds' if type_ == 'audio' else 'seed'] = [seed] if type_ == 'audio' else seed
    task_id = payload.get('id') or new_task_id()
    if not is_valid_id(task_id):
        raise ApiError(422, 'REQUEST_INVALID', '任务 ID 不合法')
    existing = db.get_task(task_id)
    if existing and (not allow_existing or existing.get('status') == 'running'
                     or existing.get('type') != type_):
        raise ApiError(409, 'TASK_EXISTS', '任务 ID 已存在或仍在运行')
    parent_id = payload.get('parentId')
    if parent_id:
        parent = db.get_task(parent_id)
        if not parent or parent.get('type') != type_ or not parent.get('protocol'):
            raise ApiError(422, 'CONFIG_INVALID', '训练协议复评来源无效')
    return {
        'id': task_id, 'name': name.strip(), 'type': type_, 'mode': 'measured',
        'status': 'draft', 'step': 0, 'createdAt': now(), 'completedAt': None,
        'error': '', 'protocol': protocol, 'measurementSource': 'training_protocol',
        'source': {'kind':'training_protocol'}, 'samples': [], 'logs': [], 'feedback': [],
        'datasetId': payload.get('datasetId'), 'datasetName': payload.get('datasetName') or name.strip(),
        'datasetOrigin': {'kind':'training_protocol'}, 'parentId': parent_id,
        'seed': seed, 'config': payload.get('config') or templates.default_config(type_),
        'baselineModel': '同模型在 D_ref 上训练', 'candidateModel': '同模型在 D_ours 上训练',
        'validationVersion': payload.get('validationVersion') or 'protocol-fixed-E',
        'modelId': payload.get('modelId'), 'modelVersion': payload.get('modelVersion'),
        'planSnapshot': {'protocol': deepcopy(protocol)}, '_needsEngine': False,
    }
