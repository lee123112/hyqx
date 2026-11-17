"""可选模型后端门禁与严格解析。

本算子按 DEVELOPMENT_PLAN/TECHNICAL_DESIGN 的默认口径使用**规则后端**（backend=rule）。
仅当显式配置 backend=auto/model 时才尝试模型：

- `auto` + 模型不可用 -> 按 `fallback_policy` 降级为 rule（运行状态 degraded）或结构化失败；
- `model` + 模型不可用 -> 结构化失败（绝不静默降级为规则却仍标 model）；
- 模型返回严格解析：`consistent` 必须为布尔；`confidence` 必须为 0~1 数值或 null；
  `model_version` 必须是非空字符串或 null；非法即抛 `ModelScoreError`（逐条记 model_error）。

严禁任何固定分数、随机分数或伪造降级。
"""
import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple


class BackendUnavailable(Exception):
    """模型后端不可用（配置了但加载 / 连通失败）。"""


class ModelScoreError(Exception):
    """模型返回不合法（缺字段、类型错误、越界）。"""


def parse_model_payload(payload, fallback_version: Optional[str] = None) -> dict:
    """严格解析模型返回；任何不合规都抛 ModelScoreError，绝不填补编造值。"""
    if not isinstance(payload, dict):
        raise ModelScoreError("模型返回必须是 JSON 对象")

    if "consistent" not in payload:
        raise ModelScoreError("模型返回缺少 consistent")
    consistent = payload.get("consistent")
    if not isinstance(consistent, bool):
        raise ModelScoreError("consistent 必须是布尔值")

    confidence = payload.get("confidence", None)
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ModelScoreError("confidence 必须是 0~1 的数值或 null")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ModelScoreError("confidence 越界（必须位于 0~1）")

    version = payload.get("model_version", None)
    if version is not None:
        if not isinstance(version, str) or not version.strip():
            raise ModelScoreError("model_version 必须是非空字符串或 null")
        version = version.strip()
    if version is None:
        version = fallback_version

    evidence = payload.get("evidence", None)
    if evidence is not None and not isinstance(evidence, list):
        raise ModelScoreError("evidence 必须是数组或 null")

    return {
        "consistent": consistent,
        "confidence": confidence,
        "model_version": version,
        "evidence": evidence if isinstance(evidence, list) else [],
    }


class _HttpChecker:
    """HTTP 模型服务适配器：POST {source, target}，原样返回 JSON 交由上层严格解析。"""

    def __init__(self, url: str, timeout: float, entrypoint: str):
        self.url = url.strip()
        self.timeout = float(timeout)
        self.entrypoint = entrypoint
        self.model_version = None

    def check(self, source: str, target: str):
        body = json.dumps({"source": source, "target": target,
                           "entrypoint": self.entrypoint}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ModelScoreError(f"模型服务不可达：{type(exc).__name__}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelScoreError("模型服务返回不是合法 JSON") from exc


class _LocalChecker:
    """本地模型适配器：从 model_path 加载模块并调用 model_entrypoint。"""

    def __init__(self, model_path: str, entrypoint: str, model_version: Optional[str]):
        path = Path(model_path)
        target = path / "model.py" if path.is_dir() else path
        if not target.is_file():
            raise BackendUnavailable(f"模型文件不存在：{target}")
        spec = importlib.util.spec_from_file_location("entity_consistency_model", target)
        if spec is None or spec.loader is None:
            raise BackendUnavailable(f"无法加载模型模块：{target}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - 加载失败一律视为后端不可用
            raise BackendUnavailable(f"模型加载失败：{type(exc).__name__}") from exc
        func = getattr(module, entrypoint, None)
        if not callable(func):
            raise BackendUnavailable(f"模型缺少入口函数：{entrypoint}")
        self._func = func
        self.model_version = model_version or getattr(module, "MODEL_VERSION", None)

    def check(self, source: str, target: str):
        return self._func(source, target)


def resolve_backend(cfg, model_checker=None) -> Tuple[str, Optional[object], Optional[str]]:
    """返回 (backend, checker, degrade_reason)。

    - backend=rule：直接返回规则后端，忽略模型（规则是本算子的一等后端）；
    - backend=auto/model：注入 checker 优先；否则按配置构造；不可用时按门禁决定降级或失败。
    """
    if cfg.backend == "rule":
        return "rule", None, None

    if model_checker is not None:
        return "model", model_checker, None

    checker = None
    problem = None
    if cfg.model_service_url:
        checker = _HttpChecker(cfg.model_service_url, cfg.model_timeout_seconds, cfg.model_entrypoint)
    elif cfg.model_path:
        try:
            checker = _LocalChecker(cfg.model_path, cfg.model_entrypoint, cfg.model_version)
        except BackendUnavailable as exc:
            problem = f"model_unavailable: {exc}"
    else:
        problem = "model_unavailable: 未配置 model_service_url 或 model_path"

    if checker is not None:
        return "model", checker, None

    if cfg.backend == "model" or cfg.fallback_policy == "fail":
        raise BackendUnavailable(problem or "模型后端不可用")
    return "rule", None, problem
