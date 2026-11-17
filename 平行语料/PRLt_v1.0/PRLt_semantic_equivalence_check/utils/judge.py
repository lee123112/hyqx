"""P2 语义判别器（2026-11-05）：用本地 Qwen3.5-2B 判「译文里有没有原文根本没有的专名/实体」。

【为什么需要】
    算子14 的语义分是 Qwen3-Embedding-0.6B 的**整句**余弦。整句余弦对局部替换不敏感：
    Fullflow-Effect-Bench-v1 里 17 条实体替换（把某个词换成原文根本没有的人名）
    父句分 0.652–0.815，全部高于阈值 0.65，规则守卫（数字/单位/否定/完整性）也看不见
    ——数字没变、单位没变、否定没变、句子完整。词法层没有可用的通用信号：
    中文译名（胡纳 → Huhne）与英文人名之间没有可查的对照表，硬做专名白名单只会误拦。

【口径：先对账，再判拦（2026-11-05 第二轮）】
    第一版提示词让模型直接列「原文没有的名字」，自然语料误拦率 20.7%（150 条抽样 31 条）：
    模型认不出 钱伯斯→Chambers / 南非→South Africa 这类译名，把合法专名报成缺失。
    第二版改成**先给对应关系、再判缺失**：
      - 模型必须为它在译文中认到的每个名字给出**源文里的中文片段**（`names`）；
      - 这些片段要逐字出现在源文里（去空白后比对），对不上的按「没有对应」处理
        （模型可能编造片段，编造的片段过不了这个校验）；
      - 只有模型明确列为 `missing` 的、且形状像专名的词才判拦。
    这样模型「没认出来」不再自动等于「原文没有」——它得先说出中文才行。
    实测（2026-11-05，150 条自然句对抽样 + 17 条构造负例）：
      - 第一版（直接列"原文没有的名字"）：召回 17/17，自然语料误拦 31/150 = 20.7%；
      - 第二版（先对账后判拦）：        召回 13/17，自然语料误拦 12/150 = 8.0%。
    取第二版：20.7% 的误拦会让每个交付批次丢掉约两成本来合格的记录，不可用。
    剩下 4 条漏检都是模型把植入的名字「认」成了原文里真实存在的中文名
    （如 Anderson ↔ 胡纳），因此给出了通过对账的中文片段——这类错误在词法层与
    本判别器口径下都不可见，需要在报告里如实说明，不能当成已解决。

    仍然只判一个方向（译文多出实体 → 拦）。反方向（原文有、译文没有）不判：
    中文专名常被意译/合并（「秘书处」→「the Secretariat」），这条方向误报高。

【不可用时的行为】
    权重缺失、依赖缺失、生成失败、输出不是 JSON → 一律不产生规则，
    `status` 写 unavailable/error/skipped_short，绝不用默认值代替判别结果。

【成本】
    权重 4.3GB，懒加载（只有真的需要判定时才加载）；单条约 1.2–1.5s（RTX 4060 / fp16）。
    算子14 只在语义分与全部规则守卫都放行后才调用它，因此只对「本来要交付」的记录产生开销。
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

MODEL_ID = "Qwen3.5-2B"
MODEL_VERSION = "Qwen3.5-2B-local-entity-judge-v2"
MAX_NEW_TOKENS = 96
MAX_INPUT_CHARS = 1200

SYSTEM_PROMPT = (
    "You are a strict bilingual translation checker for Chinese->English. "
    "You check ONE thing: whether every name, entity and number in the English translation "
    "has a corresponding piece of the Chinese source. "
    "Names may appear in Chinese as transliterations (for example Harrison may appear as 哈里森). "
    "Do NOT judge style, fluency or word order. Reply with ONE line of JSON only."
)

EXAMPLES = (
    "Examples (do not evaluate them):\n"
    'Chinese: 尽管天气恶劣，比赛仍照常进行。 / English: Despite the bad weather, the match went ahead. -> '
    '{"names": [], "missing": []}\n'
    'Chinese: 尽管天气恶劣，比赛仍照常进行。 / English: Harrison the bad weather, the match went ahead. -> '
    '{"names": [], "missing": ["Harrison"]}\n'
    'Chinese: 哈里森教授说，结果令人满意。 / English: Professor Harrison said the results were satisfactory. -> '
    '{"names": [{"en": "Harrison", "zh": "哈里森"}], "missing": []}\n'
    'Chinese: 史密斯已经辞职，职位由新任部长接替。 / English: Brown has resigned and will be replaced by the new minister. -> '
    '{"names": [], "missing": ["Brown"]}'
)

FORMAT_RULES = (
    'Reply exactly one line: {"names": [{"en": "...", "zh": "..."}], "missing": ["..."]}. '
    'names = English names/groups that DO appear in the Chinese, each with the exact Chinese characters. '
    'missing = English names/groups that appear nowhere in the Chinese. '
    'No quotation marks inside strings. No text outside the JSON.'
)
# [2026-11-05] 解析失败时的加严重试。实测（defect44_op14v2_20261105，9/433 = 2.1%）：
# 偶尔会把源文里的中文词逐个写进 missing（"华硕"/"个人"/"，"…），96 token 被撑满后
# 截断成非法 JSON。这只影响格式，不影响已经能正常输出的 98% 判定，所以只在解析失败后
# 加这一次提示重试一次，不改主口径，也不改动召回/误报的实测值。
STRICT_HINT = (' The answer must be ONE line of English JSON. missing may contain only '
               'English names; never put Chinese characters in missing.')

# 常见「句首大写但不是专名」的词：只影响「可疑词要不要当真」，命中直接忽略，不会因此拦记录。
SENTENCE_START_STOP = frozenset({
    "mindful", "noting", "recalling", "recognizing", "welcoming", "considering",
    "emphasizing", "bearing", "having", "taking", "deeply", "fully", "concerned",
    "alarmed", "acknowledging", "aware", "convinced", "determined", "guided",
    "reaffirming", "stressing", "underlining", "urging", "conscious", "further",
    "also", "however", "therefore", "moreover", "according", "welcome", "education",
    "cognizant", "tuesday", "monday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
})

# 联合国/公文里的句首动词：「Finds that…」「Recommends that…」不是专名。
# 只收出现在句首、且几乎不会当专名用的动词，宁可漏收（漏收只会多拦，不会漏拦）。
DOC_VERBS = frozenset({
    "finds", "decides", "notes", "recommends", "requests", "calls", "urges",
    "approves", "endorses", "recognizes", "recognises", "reaffirms", "reiterates",
    "encourages", "invites", "expresses", "condemns", "demands", "adopts",
    "concludes", "observes", "recalls", "acknowledges", "welcomes", "supports",
    "affirms", "confirms", "resolves", "declares", "states", "notes",
    "authorizes", "authorises", "decides", "designates", "elects", "appoints",
})

# 片段/短语的起始词：以这些词开头的多词可疑项不是专名（"As a result"、"The hour long course"）
_PHRASE_START_STOP = frozenset({
    "the", "a", "an", "as", "in", "on", "at", "for", "with", "by", "to", "and",
    "but", "or", "no", "not", "her", "his", "its", "their", "our", "my", "this",
    "that", "these", "those", "it", "we", "they", "he", "she", "there", "here",
    "all", "some", "many", "most", "such", "any", "each", "every", "one", "two",
})

_MODEL: Any = None
_TOKENIZER: Any = None
_LOAD_LOCK = threading.Lock()


class JudgeUnavailable(RuntimeError):
    """判别器不可用（权重缺失、加载失败、生成失败、输出无法解析）。上层据此不产生规则。"""


def _load(model_dir: Path):
    """懒加载本地权重；失败抛 JudgeUnavailable（不静默降级成"无问题"）。"""
    global _MODEL, _TOKENIZER
    if _MODEL is not None and _TOKENIZER is not None:
        return _TOKENIZER, _MODEL
    with _LOAD_LOCK:
        if _MODEL is not None and _TOKENIZER is not None:
            return _TOKENIZER, _MODEL
        if not (Path(model_dir) / "config.json").is_file():
            raise JudgeUnavailable("judge_weights_missing")
        try:
            import torch  # noqa: F401  (只有真的要推理时才引入 torch)
            from transformers import AutoModelForImageTextToText, AutoTokenizer
        except Exception as exc:  # noqa: BLE001
            raise JudgeUnavailable("judge_dependency_missing") from exc
        try:
            tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
            model = AutoModelForImageTextToText.from_pretrained(
                str(model_dir), local_files_only=True, dtype=torch.float16,
                low_cpu_mem_usage=True)
        except Exception as exc:  # noqa: BLE001
            raise JudgeUnavailable("judge_load_failed:" + type(exc).__name__) from exc
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = model.to(device)
        model.eval()
        _TOKENIZER, _MODEL = tokenizer, model
        return _TOKENIZER, _MODEL


def _balanced_object(text: str) -> Optional[str]:
    """取第一个括号平衡的 JSON 对象（模型偶尔带前言或代码围栏）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:pos + 1]
    return None


def _generate(source: str, target: str, model_dir: Path, strict: bool = False) -> str:
    import torch
    tokenizer, model = _load(model_dir)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join([
            EXAMPLES, FORMAT_RULES + (STRICT_HINT if strict else ""),
            "Chinese source:\n" + source[:MAX_INPUT_CHARS],
            "English translation:\n" + target[:MAX_INPUT_CHARS],
            "Reply:",
        ])},
    ]
    try:
        inputs = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt",
            return_dict=True, enable_thinking=False)
    except TypeError:  # 模板不支持 enable_thinking 时退回默认
        inputs = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
    device = next(model.parameters()).device
    inputs = {name: value.to(device) for name, value in inputs.items()}
    input_len = inputs["input_ids"].shape[-1]
    with torch.inference_mode():
        generated = model.generate(
            **inputs, max_new_tokens=MAX_NEW_TOKENS, num_beams=1, do_sample=False,
            pad_token_id=getattr(tokenizer, "pad_token_id", None) or getattr(tokenizer, "eos_token_id", None))
    return tokenizer.decode(generated[0][input_len:], skip_special_tokens=True).strip()


def _norm_chars(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def name_like_suspects(suspects: Any, target: str) -> List[str]:
    """只保留「专名形状」的可疑词：首字母大写、具备字母、在译文里出现、不在句首/公文动词表里。

    这层过滤是为了精度：2B 模型会把 gender / finance / Finds / As a result 这类普通词
    报成"原文没有"，它们不是实体替换，不该拦。过滤只会让判别器更保守（少拦，不多拦）。
    """
    kept: List[str] = []
    if not isinstance(suspects, list):
        return kept
    for item in suspects:
        if not isinstance(item, str):
            continue
        token = item.strip().strip("\"'").strip()
        if len(token) < 2 or not token[0].isupper() or not re.search(r"[A-Za-z]", token):
            continue
        lowered = token.lower()
        if lowered in SENTENCE_START_STOP:
            continue
        words = lowered.split()
        if words and (words[0] in DOC_VERBS or (len(words) > 1 and words[0] in _PHRASE_START_STOP)):
            continue
        if not re.search(r"(?<![A-Za-z])" + re.escape(token), target or ""):
            continue
        if token not in kept:
            kept.append(token)
    return kept


def verify_payload(payload: Any, source: str, target: str) -> Dict[str, Any]:
    """把模型输出对账成判定结果：``names`` 里的中文必须在源文里真的出现，``missing`` 才判拦。

    返回 ``{"rules", "suspects", "unverified", "missing", "confidence", "model_version"}``。
    ``unverified`` 是模型声称有对应、但给出的中文片段在源文里找不到的条目
    （模型编造片段时多半属于这种情况，按"没有对应"处理）。
    """
    if not isinstance(payload, dict):
        raise JudgeUnavailable("judge_output_not_object")
    src = _norm_chars(source)

    unverified: List[Dict[str, Any]] = []
    for pair in payload.get("names") or []:
        if not isinstance(pair, dict):
            continue
        en, zh = pair.get("en"), pair.get("zh")
        if not isinstance(en, str):
            continue
        if not isinstance(zh, str) or not zh.strip():
            unverified.append({"en": en, "zh": zh, "why": "no_chinese_given"})
        elif _norm_chars(zh) not in src:
            unverified.append({"en": en, "zh": zh, "why": "quote_not_in_source"})

    # 兼容第一版输出形状（faithful/suspect）：老形状里 suspect 等价于 missing
    missing_raw = payload.get("missing")
    if missing_raw is None:
        missing_raw = payload.get("suspect")
    missing = name_like_suspects(missing_raw, target)

    suspects: List[str] = []
    for en in missing + [item["en"] for item in unverified]:
        if en not in suspects:
            suspects.append(en)

    confidence = payload.get("confidence")
    if isinstance(confidence, str):
        try:
            confidence = float(confidence.strip())
        except ValueError:
            confidence = None

    return {
        "rules": ["entity_not_in_source"] if suspects else [],
        "suspects": suspects,
        "missing": missing,
        "unverified": unverified,
        "confidence": confidence if isinstance(confidence, (int, float)) else None,
        "model_version": MODEL_VERSION,
    }


def judge_entity_support(source: str, target: str, model_dir: Path,
                         strict: bool = False) -> Dict[str, Any]:
    """判一条判定单元：命中 ``["entity_not_in_source"]`` 表示译文多出原文没有的专名。

    权重/解析故障抛 JudgeUnavailable，调用方不得把它当成"没问题"。
    ``strict`` 只用于解析失败后的那一次重试（见 STRICT_HINT）。
    """
    raw = _generate(source, target, model_dir, strict=strict)
    candidate = _balanced_object(raw)
    if candidate is None:
        raise JudgeUnavailable("judge_output_not_json")
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise JudgeUnavailable("judge_output_not_json") from exc
    return verify_payload(payload, source, target)


class EntityJudge:
    """判别器的调用包装：只在启用且权重存在时构造，逐条调用并统计开销。"""

    def __init__(self, model_dir: Path, min_chars: int = 12):
        self.model_dir = Path(model_dir)
        self.min_chars = int(min_chars)
        self.calls = 0
        self.errors = 0
        self.retries = 0
        self.last_error: Optional[str] = None

    def available(self) -> bool:
        return (self.model_dir / "config.json").is_file()

    def judge(self, source: str, target: str) -> Dict[str, Any]:
        """返回判别结果；不可用/失败时返回 ``{"rules": [], "status": ...}``，不抛给主流程。"""
        if len("".join((target or "").split())) < self.min_chars:
            return {"rules": [], "status": "skipped_short", "suspects": []}
        if not self.available():
            return {"rules": [], "status": "unavailable", "suspects": []}
        self.calls += 1
        try:
            result = judge_entity_support(source, target, self.model_dir)
        except JudgeUnavailable as exc:
            # 解析失败多半是模型把中文词写进 missing 撑爆输出，加严重试一次
            self.retries += 1
            try:
                result = judge_entity_support(source, target, self.model_dir, strict=True)
            except JudgeUnavailable as exc2:
                self.errors += 1
                self.last_error = f"{exc} / retry:{exc2}"
                return {"rules": [], "status": "error", "suspects": [], "error": str(exc2)}
        result["status"] = "judged"
        return result
