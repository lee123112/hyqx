"""领域识别后端适配、规则关键词证据与语言对归一化。

设计约束（对应 DEVELOPMENT_PLAN / TECHNICAL_DESIGN）：

- 首选后端为本地 Qwen3-Embedding-0.6B 零样本原型适配器（或配置的 HTTP 服务）：
  通过配置的本地适配器返回 0–1 的真实 `domain_confidence`。本模块只做接线、范围校验与
  转发，**绝不生成、映射或补全任何置信度**。
- 模型不可用时返回 `BackendUnavailable`，由上层决定降级或结构化失败；降级后端
  （`rule_degraded`）只输出真实关键词命中证据，`domain_confidence` 恒为 None，
  绝不用随机数、固定 0.5 或文本长度伪造置信度。
"""
import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import request as urlrequest


def _load_local_env() -> None:
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        break

MAX_EVIDENCE_PER_CANDIDATE = 20

# 语言对归一时使用的已知基础语言代码（用于把地区子标签并入同一侧）。
_BASE_LANGS = {
    "zh", "en", "ja", "ko", "fr", "de", "es", "ru", "ar", "pt", "it", "th", "vi",
    "id", "ms", "hi", "tr", "nl", "pl", "sv", "fi", "da", "no", "el", "cs", "uk",
    "he", "fa", "bn", "ta", "ur", "my", "km", "lo", "ne", "si", "mn", "kk", "uz",
}
# 常见地区/书写体系子标签归并到基础语言。
_LANG_ALIASES = {
    "zh-cn": "zh", "zh-tw": "zh", "zh-hk": "zh", "zh-sg": "zh",
    "zh-hans": "zh", "zh-hant": "zh",
    "en-us": "en", "en-gb": "en", "en-au": "en", "en-ca": "en", "en-in": "en",
}
_PAIR_SEPARATORS = ("→", "=>", "->", "|", "/", ":", "_", "~")


class BackendUnavailable(Exception):
    """模型后端不可用：未配置、权重缺失、服务不可达或依赖缺失。"""


class ModelScoreError(Exception):
    """模型对单条记录返回了不可用结果（缺失、非数值、越界或调用异常）。"""


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #

def _normalize_side(tokens: List[str]) -> Optional[str]:
    if not tokens:
        return None
    full = "-".join(tokens)
    if full in _LANG_ALIASES:
        return _LANG_ALIASES[full]
    return tokens[0]


def normalize_language_pair(value) -> Optional[str]:
    """把 zh-en / zh_en / zh->en / zh→en / zh-cn-en-us 等写法归一为 `zh-en`。"""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    for separator in _PAIR_SEPARATORS:
        text = text.replace(separator, "-")
    tokens = [token for token in text.split("-") if token]
    if not tokens:
        return None

    sides: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if current and token in _BASE_LANGS and current[0] in _BASE_LANGS:
            sides.append(current)
            current = [token]
        else:
            current.append(token)
    if current:
        sides.append(current)

    if len(sides) != 2:
        return None
    left = _normalize_side(sides[0])
    right = _normalize_side(sides[1])
    if not left or not right or left == right:
        return None
    return f"{left}-{right}"


def normalize_domain(value, aliases: Optional[Dict[str, str]] = None) -> Optional[str]:
    """领域标签归一：去空白、小写，并按别名表映射到规范领域名。"""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if aliases:
        mapped = aliases.get(text)
        if isinstance(mapped, str) and mapped.strip():
            return mapped.strip().lower()
    return text


# --------------------------------------------------------------------------- #
# 规则关键词证据（只产证据，不产置信度）
# --------------------------------------------------------------------------- #

def _rule_evidence(field: str, keyword: str, domain: str) -> Dict[str, str]:
    return {
        "type": "keyword",
        "field": field,
        "keyword": keyword,
        "domain": domain,
        "backend": "rule_degraded",
    }


def _normalized_subdomain_keywords(cfg) -> Dict[str, Dict[str, List[str]]]:
    aliases = cfg.domain_aliases or {}
    result: Dict[str, Dict[str, List[str]]] = {}
    for raw_domain, mapping in (cfg.subdomain_keywords or {}).items():
        domain = normalize_domain(raw_domain, aliases)
        if not domain or not isinstance(mapping, dict):
            continue
        bucket = result.setdefault(domain, {})
        for raw_subdomain, keywords in mapping.items():
            subdomain = normalize_domain(raw_subdomain, None)
            if subdomain and isinstance(keywords, list):
                bucket[subdomain] = [k for k in keywords if isinstance(k, str) and k.strip()]
    return result


def rule_candidates(fields: Dict[str, str], cfg) -> List[Dict[str, Any]]:
    """按任务显式词表统计真实关键词命中，返回候选领域（无任何置信度）。"""
    aliases = cfg.domain_aliases or {}
    subdomain_map = _normalized_subdomain_keywords(cfg)
    buckets: Dict[str, Dict[str, Any]] = {}

    for field in cfg.text_fields:
        text = fields.get(field)
        if not isinstance(text, str) or not text:
            continue
        lowered = text.lower()
        for raw_domain, keywords in (cfg.domain_keywords or {}).items():
            domain = normalize_domain(raw_domain, aliases)
            if not domain or not isinstance(keywords, list):
                continue
            bucket = buckets.setdefault(domain, {"matched": set(), "evidence": []})
            for keyword in keywords:
                if not isinstance(keyword, str):
                    continue
                token = keyword.strip()
                if not token:
                    continue
                key = token.lower()
                if key in lowered and key not in bucket["matched"]:
                    bucket["matched"].add(key)
                    if len(bucket["evidence"]) < MAX_EVIDENCE_PER_CANDIDATE:
                        bucket["evidence"].append(_rule_evidence(field, token, domain))

    candidates: List[Dict[str, Any]] = []
    for domain, bucket in buckets.items():
        if not bucket["matched"]:
            continue
        subdomain = None
        bucket_hits: Dict[str, int] = {}
        for field in cfg.text_fields:
            text = fields.get(field)
            if not isinstance(text, str) or not text:
                continue
            lowered = text.lower()
            for candidate_subdomain, keywords in subdomain_map.get(domain, {}).items():
                for keyword in keywords:
                    if keyword.lower() in lowered:
                        bucket_hits[candidate_subdomain] = bucket_hits.get(candidate_subdomain, 0) + 1
        if bucket_hits:
            subdomain = sorted(bucket_hits.items(), key=lambda item: (-item[1], item[0]))[0][0]
        candidates.append({
            "domain": domain,
            "subdomain": subdomain,
            "confidence": None,
            "hits": len(bucket["matched"]),
            "evidence": list(bucket["evidence"]),
        })

    candidates.sort(key=lambda item: (-item["hits"], item["domain"]))
    return candidates[: cfg.max_candidates]


# --------------------------------------------------------------------------- #
# 模型后端
# --------------------------------------------------------------------------- #

def _parse_candidate(item: Any, aliases) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise ModelScoreError("候选领域不是 JSON 对象")
    domain = normalize_domain(item.get("domain"), aliases)
    if not domain:
        raise ModelScoreError("候选领域缺少有效 domain")
    confidence = item.get("domain_confidence", item.get("confidence"))
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ModelScoreError("候选领域缺少数值型置信度")
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise ModelScoreError(f"候选领域置信度 {confidence} 超出 [0, 1]")
    subdomain = item.get("subdomain")
    if subdomain is not None and not isinstance(subdomain, str):
        raise ModelScoreError("候选领域 subdomain 必须是字符串或 null")
    return {
        "domain": domain,
        "subdomain": normalize_domain(subdomain, None),
        "confidence": confidence,
    }


def _parse_model_payload(payload: Any, cfg, fallback_version: Optional[str]) -> Dict[str, Any]:
    """校验并原样转发模型返回值；任何异常都不得用规则结果顶替。"""
    if not isinstance(payload, dict):
        raise ModelScoreError("模型返回不是 JSON 对象")
    aliases = cfg.domain_aliases or {}

    domain = normalize_domain(payload.get("domain"), aliases)
    if not domain:
        raise ModelScoreError("模型未返回有效 domain")

    confidence = payload.get("domain_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ModelScoreError("模型未返回数值型 domain_confidence")
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise ModelScoreError(f"模型 domain_confidence={confidence} 超出 [0, 1]")

    raw_subdomain = payload.get("subdomain")
    if raw_subdomain is not None and not isinstance(raw_subdomain, str):
        raise ModelScoreError("模型 subdomain 必须是字符串或 null")
    subdomain = normalize_domain(raw_subdomain, None)

    raw_candidates = payload.get("candidates")
    if raw_candidates is None:
        raw_candidates = []
    if not isinstance(raw_candidates, list):
        raise ModelScoreError("模型 candidates 不是列表")
    candidates = [_parse_candidate(item, aliases) for item in raw_candidates]

    evidence = payload.get("evidence")
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        raise ModelScoreError("模型 evidence 不是列表")

    version = payload.get("model_version") or fallback_version

    if not any(item["domain"] == domain for item in candidates):
        candidates.insert(0, {"domain": domain, "subdomain": subdomain, "confidence": confidence})
    if not cfg.allow_multi_label:
        candidates = [item for item in candidates if item["domain"] == domain][:1]
    candidates = candidates[: cfg.max_candidates]

    return {
        "domain": domain,
        "subdomain": subdomain,
        "domain_confidence": confidence,
        "candidates": candidates,
        "evidence": list(evidence),
        "model_version": version,
    }


def _chat_json_payload(payload: Any) -> Dict[str, Any]:
    """Extract the JSON object returned in an OpenAI-compatible chat response."""
    if isinstance(payload, dict) and isinstance(payload.get("choices"), list):
        try:
            content = payload["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError) as exc:
            raise ModelScoreError("chat 响应缺少 choices[0].message.content") from exc
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            raise ModelScoreError("chat 响应 content 不是 JSON 文本")
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelScoreError("chat 响应 content 不是有效 JSON") from exc
        if not isinstance(result, dict):
            raise ModelScoreError("chat 响应 JSON 不是对象")
        return result
    return payload


def _parse_relevance_payload(payload: Any, fallback_version: Optional[str] = None) -> Dict[str, Any]:
    """校验关键词相关性结果；分值、关键词证据缺一即判模型错误，绝不补默认值。"""
    if not isinstance(payload, dict):
        raise ModelScoreError("相关性结果不是 JSON 对象")

    score = payload.get("relevance_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ModelScoreError("相关性结果缺少数值型 relevance_score")
    score = float(score)
    if not (0.0 <= score <= 1.0):
        raise ModelScoreError(f"relevance_score={score} 超出 [0, 1]")

    matched = payload.get("matched_keywords")
    if matched is None:
        matched = []
    if not isinstance(matched, list):
        raise ModelScoreError("matched_keywords 不是列表")
    normalized: List[Dict[str, Any]] = []
    for item in matched:
        if not isinstance(item, dict) or not isinstance(item.get("keyword"), str):
            raise ModelScoreError("matched_keywords 条目缺少 keyword")
        cosine = item.get("cosine")
        if isinstance(cosine, bool) or not isinstance(cosine, (int, float)):
            raise ModelScoreError("matched_keywords.cosine 不是数值")
        normalized.append({"keyword": item["keyword"], "cosine": float(cosine)})

    evidence = payload.get("evidence")
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        raise ModelScoreError("相关性 evidence 不是列表")

    def _optional_number(key: str) -> Optional[float]:
        value = payload.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModelScoreError(f"{key} 不是数值")
        return float(value)

    return {
        "relevance_score": score,
        "cos_pos": _optional_number("cos_pos"),
        "cos_neg": _optional_number("cos_neg"),
        "temperature": _optional_number("temperature"),
        "aggregate": payload.get("aggregate"),
        "matched_keywords": normalized,
        "relevance_score_type": payload.get("relevance_score_type"),
        "relevance_calibration": payload.get("relevance_calibration"),
        "relevance_backend": payload.get("relevance_backend") or "model",
        "keyword_count": payload.get("keyword_count"),
        "evidence": list(evidence),
        "model_version": payload.get("model_version") or fallback_version,
    }


def rule_relevance_evidence(fields: Dict[str, str], keywords: List[str]) -> List[Dict[str, Any]]:
    """规则降级下的词面命中证据：只报真实子串命中，不产生任何分数。"""
    evidence: List[Dict[str, Any]] = []
    for field in ("source", "target"):
        text = fields.get(field)
        if not isinstance(text, str) or not text:
            continue
        lowered = text.casefold()
        for keyword in keywords:
            if keyword and keyword.casefold() in lowered:
                evidence.append({
                    "type": "keyword",
                    "field": field,
                    "keyword": keyword,
                    "backend": "rule_degraded",
                })
    return evidence[:MAX_EVIDENCE_PER_CANDIDATE]


def _parse_rule_result(candidates: List[Dict[str, Any]], cfg) -> Dict[str, Any]:
    """规则降级结果：只取真实命中的候选，置信度恒为 None。"""
    if not candidates:
        return {
            "domain": None,
            "subdomain": None,
            "domain_confidence": None,
            "candidates": [],
            "evidence": [],
            "model_version": None,
        }
    primary = candidates[0]
    evidence: List[Dict[str, Any]] = []
    for item in candidates:
        evidence.extend(item["evidence"])
    return {
        "domain": primary["domain"],
        "subdomain": primary["subdomain"],
        "domain_confidence": None,
        "candidates": candidates,
        "evidence": evidence[: MAX_EVIDENCE_PER_CANDIDATE * cfg.max_candidates],
        "model_version": None,
    }


class HttpDomainClassifier:
    """通过 HTTP 调用配置的领域分类服务（可选后端；正式版默认使用本地适配器）。

    HTTP 服务只暴露分类接口，没有相关性打分入口，因此
    ``supports_relevance=False``：配置为 keyword_relevance 时按后端不可用处理，
    由 fallback_policy 决定降级或结构化失败，不会伪造分数。
    """

    supports_relevance = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.model_version = cfg.model_version

    def classify(self, source: str, target: str, language_pair: str = "zh-en") -> Dict[str, Any]:
        _load_local_env()
        configured_domains = []
        try:
            configured_domains = self.cfg.normalized_allowed_domains()
        except (AttributeError, ValueError):
            configured_domains = []
        # Keep the prompt's label set aligned with the configured admission
        # scope.  In particular, the medium TED demo explicitly includes
        # ``general``/``talks``; hard-coding only the production five domains
        # would make a valid model decision look out of scope.
        prompt_domains = []
        for value in configured_domains + [
            "medical", "legal", "finance", "technology", "education",
            "general", "talks", "sports", "entertainment", "unknown",
        ]:
            if value not in prompt_domains:
                prompt_domains.append(value)
        prompt = (
            "Classify the translation pair into one primary domain. Return ONLY a JSON object "
            "with keys domain, domain_confidence (number 0-1), subdomain (string or null), "
            "candidates (array of objects with domain, confidence, subdomain), and evidence "
            f"(array of concise strings). Use one of {', '.join(prompt_domains)}. "
            "Do not add markdown.\n"
            f"Language pair: {language_pair}\nSource: {source}\nTarget: {target}"
        )
        body = json.dumps(
            {"model": getattr(self.cfg, "model_name", "Qwen/Qwen2.5-7B-Instruct"),
             "messages": [{"role": "system", "content": "You are a strict bilingual domain classifier."},
                          {"role": "user", "content": prompt}],
             "temperature": 0,
             "response_format": {"type": "json_object"}},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urlrequest.Request(
            self.cfg.model_service_url, data=body,
            headers=headers,
        )
        try:
            with urlrequest.urlopen(request, timeout=self.cfg.model_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # 网络/超时/协议错误都不允许伪造置信度
            raise ModelScoreError("模型服务调用失败：" + type(exc).__name__) from exc
        parsed = _chat_json_payload(payload)
        result = _parse_model_payload(
            parsed, self.cfg, self.cfg.model_version or payload.get("model", None)
            if isinstance(payload, dict) else self.cfg.model_service_url)
        self.model_version = result.get("model_version") or self.model_version
        return result


class LocalDomainClassifier:
    """从 model_path 加载本地适配器模块，调用 classify_domain(source, target, language_pair)。"""

    def __init__(self, cfg):
        path = Path(cfg.model_path)
        if not path.is_absolute():
            candidates = [Path.cwd() / path, Path(__file__).resolve().parents[1] / path]
            path = next((candidate.resolve() for candidate in candidates if candidate.exists()), path)
        if not path.exists():
            raise BackendUnavailable("本地模型路径不存在：" + path.name)
        module_file = path if path.is_file() else path / "adapter.py"
        if not module_file.is_file():
            raise BackendUnavailable("本地模型目录缺少 adapter.py")
        spec = importlib.util.spec_from_file_location("bittext_domain_adapter", module_file)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise BackendUnavailable("本地模型适配器加载失败：" + type(exc).__name__) from exc
        entry = getattr(module, cfg.model_entrypoint, None)
        if not callable(entry):
            raise BackendUnavailable("本地模型适配器缺少入口函数：" + cfg.model_entrypoint)
        self.cfg = cfg
        self._entry = entry
        # 相关性打分入口（2026-11-03）。旧适配器没有该入口时如实标记为不支持，
        # 由 resolve_backend 按降级策略处理，绝不用分类置信度冒充相关性分。
        self._relevance_entry = getattr(module, getattr(cfg, "relevance_entrypoint",
                                                        "score_relevance"), None)
        # Preserve the adapter's declared model identity in lineage even when
        # the operator config leaves model_version unset.  This is provenance
        # only; the adapter output is still independently schema-validated.
        self.model_version = cfg.model_version or getattr(module, "MODEL_VERSION", None)

    @property
    def supports_relevance(self) -> bool:
        return callable(self._relevance_entry)

    def classify(self, source: str, target: str, language_pair: str = "zh-en") -> Dict[str, Any]:
        try:
            payload = self._entry(source, target, language_pair)
        except Exception as exc:
            raise ModelScoreError("本地模型推理失败：" + type(exc).__name__) from exc
        return _parse_model_payload(payload, self.cfg, self.model_version)

    def score_relevance(self, source: str, target: str, keywords: List[str],
                        language_pair: str = "zh-en", temperature: float = 0.05,
                        aggregate: str = "max") -> Dict[str, Any]:
        """调用适配器的关键词相关性入口；结果由上层按统一契约再校验。"""
        try:
            payload = self._relevance_entry(source, target, keywords, language_pair,
                                            temperature, aggregate)
        except Exception as exc:
            raise ModelScoreError("相关性推理失败：" + type(exc).__name__) from exc
        return _parse_relevance_payload(payload, self.model_version)


def build_model_classifier(cfg):
    """按配置构造模型后端；无可用模型来源时抛 BackendUnavailable。"""
    if cfg.model_service_url:
        return HttpDomainClassifier(cfg)
    if cfg.model_path:
        return LocalDomainClassifier(cfg)
    raise BackendUnavailable("未配置领域分类模型服务地址或本地模型路径")


def resolve_backend(cfg, model_classifier=None):
    """后端门禁。

    返回 (backend_name, classifier, degrade_reason)：

    - `rule_degraded` 显式配置：只做规则降级，status 记为 degraded；
    - 注入/成功构造模型：`model`；
    - auto 且模型不可用：按 fallback_policy 降级为 rule_degraded 或抛
      BackendUnavailable（结构化失败，绝不静默降级）；
    - model 显式且不可用：直接抛 BackendUnavailable。

    keyword_relevance 模式还要求后端具备 ``score_relevance`` 入口；缺失时同样按
    不可用处理（不会拿领域分类置信度冒充相关性分）。
    """
    if cfg.backend == "rule_degraded":
        return "rule_degraded", None, "backend_configured_rule_degraded"

    try:
        classifier = model_classifier if model_classifier is not None \
            else build_model_classifier(cfg)
        if getattr(cfg, "relevance_mode", "legacy_domain") == "keyword_relevance" \
                and not callable(getattr(classifier, "score_relevance", None)):
            raise BackendUnavailable("模型后端缺少关键词相关性入口 score_relevance")
    except BackendUnavailable as exc:
        if cfg.backend == "model" or cfg.fallback_policy == "fail":
            raise
        return "rule_degraded", None, "model_unavailable:" + str(exc)
    return "model", classifier, None
