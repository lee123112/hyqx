"""流畅度后端适配与规则证据。

设计约束（对应 DEVELOPMENT_PLAN / TECHNICAL_DESIGN）：

- 首选后端为本地 Qwen3.5-2B 适配器（models/Qwen3.5-2B/adapter.py）：通过配置的
  HTTP 服务或本地适配器返回 0–100 的真实分数与 grammar_errors。本模块只做接线、
  范围校验与转发，**绝不生成、映射或补全任何分数**。
- 模型不可用时返回 `BackendUnavailable`，由上层决定降级或结构化失败；
  降级后端（`rule_degraded`）只输出规则证据，`fluency_score` 恒为 null。
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


class BackendUnavailable(Exception):
    """模型后端不可用：未配置、权重缺失、服务不可达或依赖缺失。"""


class ModelScoreError(Exception):
    """模型对单条记录返回了不可用结果（缺失、非数值、越界或调用异常）。"""


# --------------------------------------------------------------------------- #
# 规则证据（只产证据，不产分数）
# --------------------------------------------------------------------------- #

INVISIBLE_RE = re.compile(
    r"[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\u3164\ufeff]"
)
REPLACEMENT_RE = re.compile(r"[\ufffd]")
# 典型 Latin-1 被当作 UTF-8 解读后的乱码前缀（如 "Ã©"、"â€"）
MOJIBAKE_RE = re.compile(r"(?:[\u00c2-\u00c3\u00e2][\u0080-\u00bf])")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
REPEATED_PUNCT_RE = re.compile(r"([!?.,;:~*_=\-])\1{2,}")
# 连续标点串：忽略空白后 >= 3 个半角句读符号连排（如 "text. ,, .. ??"）。
# 全句点串（"..." 省略号）与 "?!"、"!!"、"ibid., para." 这类两符号组合不算。
PUNCT_RUN_RE = re.compile(r"[,.!?;:](?:\s*[,.!?;:])+")
LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}", "（": "）", "【": "】", "《": "》"}
QUOTE_CHARS = ('"', "'", "“", "”", "‘", "’")
MIN_TEXT_CHARS = 3


def _evidence(kind: str, detail: str) -> Dict[str, str]:
    return {"type": kind, "detail": detail}


def _unbalanced_brackets(text: str) -> bool:
    stack: List[str] = []
    for char in text:
        if char in BRACKET_PAIRS:
            stack.append(BRACKET_PAIRS[char])
        elif char in BRACKET_PAIRS.values():
            if not stack or stack.pop() != char:
                return True
    return bool(stack)


def _repeated_token(text: str) -> Optional[str]:
    tokens = LATIN_TOKEN_RE.findall(text.lower())
    run = 1
    for previous, current in zip(tokens, tokens[1:]):
        run = run + 1 if previous == current else 1
        if run >= 4:
            return current
    return None


def punctuation_run(text) -> Optional[str]:
    """返回第一处连续标点串（去空白后原文），没有则 None。

    口径：半角 `, . ! ? ; :` 连排（允许中间夹空白）且去空白后长度 >= 3，
    且不全为句点（排除 "..." 省略号）。实测自然语料 2253 条 0 命中；
    构造缺陷 "译文 ,, .. ??" 全部命中。
    """
    if not isinstance(text, str):
        return None
    for match in PUNCT_RUN_RE.finditer(text):
        marks = re.sub(r"\s+", "", match.group(0))
        if len(marks) >= 3 and set(marks) != {"."}:
            return marks
    return None


def rule_evidence(text, target_lang: str = "en") -> List[Dict[str, str]]:
    """规则型基础检查：只返回证据，永远不返回分数。"""
    evidence: List[Dict[str, str]] = []
    if not isinstance(text, str):
        return [_evidence("invalid_text", "文本字段不是字符串")]

    stripped = text.strip()
    if not stripped:
        evidence.append(_evidence("empty_text", "译文为空或仅含空白"))
        return evidence

    invisible = INVISIBLE_RE.findall(text)
    if invisible:
        evidence.append(_evidence("invisible_characters", f"包含 {len(invisible)} 个不可见格式字符"))

    replacement = REPLACEMENT_RE.findall(text)
    if replacement:
        evidence.append(_evidence("replacement_character", f"包含 {len(replacement)} 个替换字符 U+FFFD"))

    if MOJIBAKE_RE.search(text):
        evidence.append(_evidence("mojibake_pattern", "存在疑似编码错位乱码片段"))

    if target_lang.lower().startswith("en"):
        cjk = CJK_RE.findall(text)
        if cjk:
            evidence.append(_evidence("untranslated_source_script", f"英文译文中残留 {len(cjk)} 个中日韩字符"))

    abnormal = REPEATED_PUNCT_RE.findall(text)
    if abnormal:
        evidence.append(_evidence("abnormal_punctuation", f"存在 {len(abnormal)} 处连续重复标点"))

    run = punctuation_run(text)
    if run:
        evidence.append(_evidence("punctuation_run", f"存在连续标点串：{run}"))

    if _unbalanced_brackets(text):
        evidence.append(_evidence("unbalanced_brackets", "括号不配对"))

    odd_quotes = sum(text.count(char) for char in QUOTE_CHARS) % 2
    if odd_quotes:
        evidence.append(_evidence("unbalanced_quotes", "引号数量为奇数，疑似不配对"))

    repeated = _repeated_token(text)
    if repeated:
        evidence.append(_evidence("repeated_token", f"存在连续重复词：{repeated}"))

    if len(stripped) < MIN_TEXT_CHARS:
        evidence.append(_evidence("suspiciously_short", f"文本长度仅 {len(stripped)} 个字符"))

    return evidence


# --------------------------------------------------------------------------- #
# 模型后端
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 缺陷主张（defect_count_v1）：严重度表、机械核验与规则缺陷
#
# 设计要点：模型只提「我曾在这里看到错误」这一条主张，是否成立由本模块核验。
# 没通过核验的主张不进分数——这是「不许先给分再编理由」的结构保证。
# --------------------------------------------------------------------------- #

# 严重度只影响计分权重；是否计分由 config.scored_defect_types 决定。
DEFECT_SEVERITY: Dict[str, str] = {
    "agreement": "hard",
    "verb_form": "hard",
    "dup_word": "minor",
    "punctuation": "minor",
    "fragment": "hard",
    "article": "minor",
    "preposition": "minor",
    "collocation": "minor",
    "word_order": "hard",
    "other": "minor",
}

AUX_FORMS = {"am", "is", "are", "was", "were", "be", "been", "being",
             "has", "have", "had", "do", "does", "did"}

# 规则证据 → 缺陷。只映射「规则本身已确证、且原本就在规则门禁管辖内」的三类；
# 编码类问题（乱码/不可见字符/括号不配对）仍只作 evidence —— 那是 2/3 号算子的职责，
# 在流畅度分里重复扣分会让同一处问题在不同算子间反复计价。
RULE_DEFECT_MAP = {
    "punctuation_run": "punctuation",
    "abnormal_punctuation": "punctuation",
    "repeated_token": "dup_word",
}

_WS_RE = re.compile(r"\s+")


def _norm_ws(value: Any) -> str:
    """归一化空白，用于 span 与原文的逐字比对（不改大小写、不改词形）。"""
    if not isinstance(value, str):
        return ""
    return _WS_RE.sub(" ", value).strip()


def _tokens(value: str) -> List[str]:
    return [token for token in _WS_RE.split(value.strip()) if token]


def token_edit_distance(left: List[str], right: List[str], limit: int) -> int:
    """token 级 Levenshtein；超过 limit 时返回 limit + 1（调用方只关心「是否超限」）。"""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i] + [0] * len(right)
        for j, b in enumerate(right, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1,
                             previous[j - 1] + (0 if a.lower() == b.lower() else 1))
        previous = current
    return previous[-1]


def _number_edit(a: str, b: str) -> bool:
    """a→b 是否是一步「数」变形（is/are、has/have、grants/grant …）。"""
    la, lb = a.lower(), b.lower()
    if la in AUX_FORMS and lb in AUX_FORMS:
        return True
    for suffix in ("s", "es"):
        if la == lb + suffix or lb == la + suffix:
            return True
    for left, right in ((la, lb), (lb, la)):
        if left.endswith("ies") and left[:-3] + "y" == right:
            return True
    return False


def _check_agreement(span: List[str], fix: List[str]) -> bool:
    """主谓/数一致：只允许一个 token 变化，且变化是数变形。

    这里验的是「主张是否具体且可证伪」（有定位、有最小修正），**不是**替代语法分析器。
    因此仍需要 config.scored_defect_types 白名单与类型有效性闸兜底。
    """
    if len(span) != len(fix):
        return False
    diff = [(a, b) for a, b in zip(span, fix) if a.lower() != b.lower()]
    return len(diff) == 1 and _number_edit(*diff[0])


def _check_verb_form(span: List[str], fix: List[str]) -> bool:
    """时态/助动词：至少一个 be/have/do 形式被加入或删除。"""
    span_set = {token.lower() for token in span}
    fix_set = {token.lower() for token in fix}
    return bool((fix_set - span_set) & AUX_FORMS) or bool((span_set - fix_set) & AUX_FORMS)


def _check_dup_word(span: List[str], _fix: List[str]) -> bool:
    """span 内是否有相邻的同一个词。

    比较前剥掉首尾标点：重复词常紧跟标点（"points points:"、"Syria Syria."），
    不剥会让这类真实重复被判成 mechanical_check_failed。
    """
    core = [token.strip(".,;:!?\"'“”‘’()[]{}") for token in span]
    return any(a and a.lower() == b.lower() for a, b in zip(core, core[1:]))


def _check_punctuation(span_text: str, _fix_text: str) -> bool:
    return bool(PUNCT_RUN_RE.search(span_text)) or bool(re.search(r"[,.!?;:]{2,}", span_text))


MECHANICAL_CHECKS = {
    "agreement": lambda span_t, fix_t, span_s, fix_s: _check_agreement(span_t, fix_t),
    "verb_form": lambda span_t, fix_t, span_s, fix_s: _check_verb_form(span_t, fix_t),
    "dup_word": lambda span_t, fix_t, span_s, fix_s: _check_dup_word(span_t, fix_t),
    "punctuation": lambda span_t, fix_t, span_s, fix_s: _check_punctuation(span_s, fix_s),
}


def verify_defects(text: str, defects: List[Any], cfg) -> tuple[List[dict], List[dict]]:
    """核验模型提出的缺陷主张，返回 (verified, suspects)。

    未通过核验的主张仍返回（带 reason），供人工复核；但它们**不参与计分**。
    """
    verified: List[dict] = []
    suspects: List[dict] = []
    normalized = _norm_ws(text)
    lowered = normalized.lower()
    for raw in defects or []:
        if not isinstance(raw, dict):
            continue
        defect_type = str(raw.get("type") or "other").strip().lower()
        span = _norm_ws(raw.get("span"))
        fix = _norm_ws(raw.get("fix"))
        item = {"type": defect_type, "span": span, "fix": fix,
                "severity": DEFECT_SEVERITY.get(defect_type, "minor"),
                "source": raw.get("source") or "model"}
        reason = None
        if not span or span.lower() not in lowered:
            reason = "span_not_found"
        elif not fix or fix.lower() == span.lower():
            reason = "non_minimal_edit"
        elif token_edit_distance(_tokens(span), _tokens(fix),
                                 cfg.max_fix_edit_distance) > cfg.max_fix_edit_distance:
            reason = "non_minimal_edit"
        elif defect_type not in cfg.scored_defect_types:
            reason = "type_not_scored"
        elif defect_type in MECHANICAL_CHECKS and not MECHANICAL_CHECKS[defect_type](
                _tokens(span), _tokens(fix), span, fix):
            reason = "mechanical_check_failed"
        if reason:
            suspects.append({**item, "verified": False, "reason": reason})
        else:
            verified.append({**item, "verified": True})
    return verified, suspects


def rule_defects(evidence: List[dict]) -> List[dict]:
    """把规则证据转成已确证的缺陷条目（不产生分数，只提供可计数的条目）。"""
    defects: List[dict] = []
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        defect_type = RULE_DEFECT_MAP.get(str(item.get("type")))
        if not defect_type:
            continue
        defects.append({
            "type": defect_type,
            "span": str(item.get("detail") or "")[:200],
            "fix": "",
            "severity": DEFECT_SEVERITY.get(defect_type, "minor"),
            "source": "rule",
            "verified": True,
        })
    return defects


def _parse_defect_payload(payload: Any, fallback_version: Optional[str]) -> Dict[str, Any]:
    """校验缺陷主张的结构完整性；不判断主张本身是否成立（那是 verify_defects 的事）。"""
    items = payload.get("defects")
    if items is None:
        items = payload.get("errors")
    if items is None:
        raise ModelScoreError("模型未返回 defects 列表")
    if not isinstance(items, list):
        raise ModelScoreError("模型 defects 不是列表")
    defects: List[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        span = item.get("span")
        if not isinstance(span, str) or not span.strip():
            continue
        defects.append({
            "type": str(item.get("type") or "other").strip().lower(),
            "span": span.strip(),
            "fix": str(item.get("fix") or "").strip(),
        })
    return {
        "defects": defects,
        "model_version": payload.get("model_version") or fallback_version,
        "score_semantics": payload.get("score_semantics") or "defect_count_v1",
        "business_gate_approved": payload.get("business_gate_approved"),
        "taxonomy_version": payload.get("taxonomy_version"),
        "model_output_repaired": payload.get("model_output_repaired"),
    }


def _parse_model_payload(payload: Any, cfg, fallback_version: Optional[str]) -> Dict[str, Any]:
    """校验并原样转发模型返回值；任何异常都不得用规则结果顶替。"""
    if not isinstance(payload, dict):
        raise ModelScoreError("模型返回不是 JSON 对象")
    if "defects" in payload or "errors" in payload:
        return _parse_defect_payload(payload, fallback_version)
    score = payload.get("fluency_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ModelScoreError("模型未返回数值型 fluency_score")
    score = float(score)
    if not (cfg.score_min <= score <= cfg.score_max):
        raise ModelScoreError(f"模型 fluency_score={score} 超出 [{cfg.score_min}, {cfg.score_max}]")

    errors = payload.get("grammar_errors")
    if errors is None:
        errors = []
    elif isinstance(errors, str):
        errors = [] if errors.strip().lower() in {"none", "无", "n/a", "null"} else [errors]
    if not isinstance(errors, list):
        raise ModelScoreError("模型 grammar_errors 不是列表")

    evidence = payload.get("evidence")
    if evidence is None:
        evidence = []
    elif isinstance(evidence, str):
        evidence = [] if not evidence.strip() else [evidence]
    if not isinstance(evidence, list):
        raise ModelScoreError("模型 evidence 不是列表")

    version = payload.get("model_version") or fallback_version
    return {
        "fluency_score": score,
        "grammar_errors": errors,
        "evidence": evidence,
        "model_version": version,
        "score_semantics": payload.get("score_semantics") or "model_score_0_100",
        "business_gate_approved": payload.get("business_gate_approved"),
        # 适配器是否因首次解析失败重试过一次（分数仍是模型真实输出，不是修补出来的）；
        # 不提供该字段的后端返回 None，表示「不适用」而不是「未重试」。
        "model_output_repaired": payload.get("model_output_repaired"),
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


class HttpModelScorer:
    """通过 HTTP 调用配置的 Qwen3.5-2B 流畅度服务。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.model_version = cfg.model_version

    def score(self, text: str, target_lang: str = "en") -> Dict[str, Any]:
        _load_local_env()
        prompt = (
            "Evaluate translation fluency. Return ONLY a JSON object with keys "
            "fluency_score (number 0-100), grammar_errors (array of concise strings), "
            "and evidence (array of concise strings). Do not add markdown.\n"
            f"Target language: {target_lang}\nTranslation: {text}"
        )
        body = json.dumps(
            {"model": getattr(self.cfg, "model_name", "Qwen3.5-2B"),
             "messages": [{"role": "system", "content": "You are a strict translation quality evaluator."},
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
        except Exception as exc:  # 网络/超时/协议错误都不允许伪造分数
            raise ModelScoreError("模型服务调用失败：" + type(exc).__name__) from exc
        parsed = _chat_json_payload(payload)
        provider_model = payload.get("model") if isinstance(payload, dict) else None
        result = _parse_model_payload(
            parsed, self.cfg, self.cfg.model_version or provider_model or self.cfg.model_service_url)
        self.model_version = result.get("model_version") or self.model_version
        return result

    def score_batch(self, texts: List[str], target_lang: str = "en") -> List[Any]:
        """HTTP 服务端暂不假设批量接口：逐条调用，异常按元素返回。"""
        outputs: List[Any] = []
        for text in texts:
            try:
                outputs.append(self.score(text, target_lang))
            except Exception as exc:
                outputs.append(exc)
        return outputs


class LocalModelScorer:
    """从 model_path 加载本地适配器模块，调用 model_entrypoint(text, target_lang)。"""

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
        spec = importlib.util.spec_from_file_location("bittext_qwen25_adapter", module_file)
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
        # 适配器可选提供 <入口名>_batch(texts, target_lang) 批量入口（如 score_fluency_batch）；
        # 缺失时 score_batch 回退为逐条循环，行为与旧版完全一致
        batch_entry = getattr(module, cfg.model_entrypoint + "_batch", None)
        self._batch_entry = batch_entry if callable(batch_entry) else None
        self.model_version = cfg.model_version or getattr(module, "MODEL_VERSION", None)

    def score(self, text: str, target_lang: str = "en") -> Dict[str, Any]:
        try:
            payload = self._entry(text, target_lang)
        except Exception as exc:
            raise ModelScoreError("本地模型推理失败：" + type(exc).__name__) from exc
        return _parse_model_payload(payload, self.cfg, self.model_version)

    def score_batch(self, texts: List[str], target_lang: str = "en") -> List[Any]:
        """批量打分：返回与 texts 等长的列表，元素为解析后的 payload 或 Exception。

        适配器有批量入口时一次调用；没有则逐条循环。逐条异常按元素返回
        （绝不伪造分数），批量入口本身抛出的异常整体上抛，由调用方回退逐条。
        """
        if self._batch_entry is None:
            outputs: List[Any] = []
            for text in texts:
                try:
                    outputs.append(self.score(text, target_lang))
                except Exception as exc:
                    outputs.append(exc)
            return outputs
        payloads = self._batch_entry(list(texts), target_lang)
        if not isinstance(payloads, list) or len(payloads) != len(texts):
            raise ModelScoreError("本地模型批量入口返回长度与输入不符")
        outputs = []
        for payload in payloads:
            if isinstance(payload, Exception):
                outputs.append(payload)
                continue
            try:
                outputs.append(_parse_model_payload(payload, self.cfg, self.model_version))
            except Exception as exc:
                outputs.append(exc)
        return outputs


def build_model_scorer(cfg):
    """按配置构造模型后端；无可用模型来源时抛 BackendUnavailable。"""
    if cfg.model_service_url:
        return HttpModelScorer(cfg)
    if cfg.model_path:
        return LocalModelScorer(cfg)
    raise BackendUnavailable("未配置 Qwen3.5-2B 服务地址或本地模型路径")


def resolve_backend(cfg, model_scorer=None):
    """后端门禁。

    返回 (backend_name, scorer, degrade_reason)：

    - `rule_degraded` 显式配置：只做规则降级，status 记为 degraded；
    - 注入/成功构造模型：`model`；
    - auto 且模型不可用：按 fallback_policy 降级为 rule_degraded 或抛
      BackendUnavailable（结构化失败，绝不静默降级）；
    - model 显式且不可用：直接抛 BackendUnavailable。
    """
    if cfg.backend == "rule_degraded":
        return "rule_degraded", None, "backend_configured_rule_degraded"

    if model_scorer is not None:
        return "model", model_scorer, None

    try:
        scorer = build_model_scorer(cfg)
    except BackendUnavailable as exc:
        if cfg.backend == "model" or cfg.fallback_policy == "fail":
            raise
        return "rule_degraded", None, "model_unavailable:" + str(exc)
    return "model", scorer, None
