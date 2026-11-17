"""算子15（译文流畅度检测）本地 Qwen3.5-2B 流畅度适配器。

【模型事实】
    本目录权重为 Qwen3.5-2B（config: model_type=qwen3_5，
    architectures=["Qwen3_5ForConditionalGeneration"]，多模态后训练模型，
    纯文本输入可用）。因此不能用 AutoModelForCausalLM 加载，必须走
    AutoModelForImageTextToText；tokenizer 走 apply_chat_template（文本消息）。
    CPU fp16 实测：加载约 20-30s；单 token 解码约 1.5s（无 causal_conv1d /
    flash-linear-attention 加速内核），因此 max_new_tokens 必须收窄，单条
    紧凑 JSON 控制在 MAX_NEW_TOKENS 以内。

【契约（被 PRLt_fluency_check/utils/fluency.py 的 LocalModelScorer 调用）】
    - detect_defects(text, target_lang="en") -> dict          ← 2026-11-06 起默认入口
    - detect_defects_batch(texts, target_lang="en") -> list
      只返回缺陷主张 {type, span, fix}，**不返回分数**；核验与计分在算子侧完成。
    - score_fluency(text, target_lang="en") -> dict            ← 历史 rubric 口径，保留兼容
    - score_fluency_batch(texts, target_lang="en") -> list[dict | Exception]
      单条失败以 Exception 元素返回（上层记 model_error，绝不伪造分数）；
      整批级故障（权重缺失/加载失败）直接上抛，由上层回退逐条路径。
    - 返回 dict 必含：fluency_score(float 0-100)、grammar_errors(list)、
      evidence(list[dict])、model_version(str)、score_semantics、
      business_gate_approved、model_output_repaired(bool：是否走了失败重试)。
    - 严格 JSON：模型输出无法解析、分数缺失/非数值/越界、列表类型不符，
      一律抛 RuntimeError，不编造默认分、不映射规则证据为分数。

【score_semantics / 门禁说明】
    模型直接给出 0-100 的 rubric 分，不是经 Gold 校准的概率百分比，因此如实声明
    score_semantics="model_score_0_100"。该分数**未经 Gold 校准**，按规范 4.15
    「未完成 Gold 校准前不作为放行/拦截的最终依据」，business_gate_approved=False：
    算子15 只产出分数与语法证据，状态为 waiting_calibration，不参与放行/拦截判定。
    绝不声称 calibrated_probability_percent，也不冒充"已通过业务校准"。

【离线】
    只读同目录本地权重（local_files_only），HF 强制离线，不发起任何网络请求。
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 强制离线：必须在 import transformers 之前设置，确保只读本地权重、不发网络请求。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import torch  # noqa: E402  (import 顺序受离线环境影响，故意放在 os.environ 之后)
from transformers import AutoModelForImageTextToText, AutoTokenizer  # noqa: E402

HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE

MODEL_ID = "Qwen3.5-2B"
# rubric v2（2026-11-04）：把评分口径写成可执行的扣分流程（先找错、按维度扣分、
# 无错不许编错），并显式声明"只评流畅度，不评忠实度/数字/术语"，减少 2B 模型
# 在长句上给同分、在专有名词上乱扣分的问题。
RUBRIC_VERSION = "rubric-v2-qwen3.5-2b-20261104"
MODEL_VERSION = "Qwen3.5-2B-local-fluency-rubric-v2"
SCORE_SEMANTICS = "model_score_0_100"
# rubric 分未经 Gold 校准：只作辅助证据（算子15 → waiting_calibration，
# 算子17 → evidence_only_until_gold），不参与放行/拦截判定。
BUSINESS_GATE_APPROVED = False
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# Qwen3.5 在 CPU 上用 fp16（Lead 实测可加载与生成）；GPU 侧同用 fp16。
TORCH_DTYPE = torch.float16
MAX_NEW_TOKENS = 96
MAX_INPUT_CHARS = 4000
# 批量解码宽度（2026-11-05）：只有在装了 causal_conv1d / flash-linear-attention
# 时才生效，否则自动逐条——参考实现下批量反而慢约 20 倍（实测 8 条 320s vs 逐条
# 1.8s/条）。8GB 显存下 4 条是安全值，可用 FLUENCY_BATCH_SIZE 调整。
BATCH_SIZE = max(1, int(os.getenv("FLUENCY_BATCH_SIZE", "4")))
SCORE_MIN, SCORE_MAX = 0.0, 100.0

_MODEL: Any = None
_TOKENIZER: Any = None
_LOAD_LOCK = threading.Lock()

SYSTEM_PROMPT = (
    "You are a strict translation-fluency evaluator. You score ONLY the target-language "
    "translation you are given: grammar, word order, collocation and readability. "
    "You never judge meaning, faithfulness, numbers or terminology, and you never reward "
    "length or complexity. Reply with exactly ONE line of JSON only: no markdown, "
    "no explanations, no extra text."
)

# 少量带分数的校准示例：2B 级模型无示例时倾向给同分（无区分度）。
CALIBRATION_EXAMPLES = """Calibration examples (do NOT evaluate them):
- "She have went to the library yesterday." -> {"fluency_score":24,"grammar_errors":["have went"],"evidence":["subject-verb disagreement"]}
- "The results are consistent with the hypothesis." -> {"fluency_score":90,"grammar_errors":[],"evidence":["grammatical and natural"]}
- "I like very much this book." -> {"fluency_score":55,"grammar_errors":["like very much this"],"evidence":["unnatural word order"]}"""

SCORE_GUIDE = (
    "Score bands: 90-100 native-level; 75-89 fluent with minor slips; "
    "60-74 understandable but clearly non-native; 40-59 frequent errors that slow reading; "
    "20-39 severe errors; 0-19 incomprehensible."
)

# [2026-11-04 v2] 把评分口径写成可执行步骤：先列具体错误 → 按维度扣分 → 无错不许编错。
# 2B 模型在长句/专有名词上容易凭印象给同分或乱扣分，明确"只评流畅度、不评忠实度"
# 和"不奖励长度"能显著减少这类漂移。
RUBRIC_BLOCK = """Rubric (score the translation only; there is no source text to compare against):
- Grammar (0-40): agreement, tense/aspect, articles, prepositions, punctuation.
- Word order and collocation (0-30): unnatural order, wrong collocations, missing or extra words.
- Readability (0-30): can be read once without re-reading, cohesive, register fits the text.
Decision procedure:
1. Find concrete errors first and list at most 3 of them, verbatim but without quotation marks.
2. Deduct points per dimension and sum the deductions; do not start from a holistic guess.
3. If no error is found, use grammar_errors=[] and score 90-100; never invent errors to look strict.
4. Do not score meaning, faithfulness, numbers, terminology, tone or style; other checks own those.
5. Never penalise length, topic or proper nouns; a short correct sentence can score 90+."""

JSON_SCHEMA_LINE = (
    'Reply with exactly ONE line of JSON and nothing else: '
    '{"fluency_score": <number 0-100>, "grammar_errors": [<short strings>], '
    '"evidence": [<short strings>]}'
)

# [2026-11-02 B2] 实测 9/402 条解析失败，全部是 evidence 里对原文词语加了单引号
# （如 before 'game'），而这段是双引号 JSON 字符串——含引号的字符串要么把 JSON 结构
# 提前截断、要么让括号配对错位，_parse_payload 判定为非法 JSON。
# 实测：加上下面两行约束后同样 9 条全部可解析。
# [2026-11-04 v2] 再补"分数必须是 JSON 数字""无错误用空数组""JSON 前后不得有文字"，
# 对应实测中模型把分数写成字符串、把无错误写成 none 的情况。
JSON_FORMAT_RULES = (
    "Format rules (mandatory):\n"
    "1. Output exactly ONE line. Never put a line break inside the JSON.\n"
    "2. fluency_score must be a JSON number such as 87 or 61.5, never a string like \"87\".\n"
    "3. Every string value must be plain text: do NOT use quotation marks "
    "(single ' or double \") or apostrophes inside any string value.\n"
    "4. Use [] when there is nothing to report; do not write none or n/a.\n"
    "5. Refer to words directly, for example: before the word game.\n"
    "6. Do not add any text before or after the JSON object."
)


def _user_prompt(text: str, target_lang: str) -> str:
    language = (target_lang or "en").strip() or "en"
    return (
        CALIBRATION_EXAMPLES + "\n" + RUBRIC_BLOCK + "\n" + SCORE_GUIDE + "\n"
        + JSON_FORMAT_RULES + "\n\n"
        f"Evaluate the following {language} translation for grammar, naturalness and readability.\n"
        + JSON_SCHEMA_LINE + "\n"
        "Keep grammar_errors and evidence short (at most 3 items each).\n"
        "Translation to evaluate:\n" + text
    )


# --------------------------------------------------------------------------- #
# 缺陷提取口径（defect_count_v1，2026-11-06）
#
# 为什么换口径：rubric-v2 让模型直接给 0-100 整体印象分，实测在 942 条自然句上只
# 吐出 16 个不同分值、71.9% 集中在 95/78 两点；被判低分的记录里模型给出的理由是
# 事后倒推出来的（"Mendoza was gunned down." 判 20 分，理由 wrong collocation）。
# 只要输出里存在一个「分数」槽位，模型就会先填分数再编理由——改 rubric 改不掉。
#
# 新契约：模型不打分，只提「类型 + 原文逐字定位 + 最小修正」的缺陷主张；没有缺陷
# 就返回空列表。计分由算子按核验通过的条数完成（utils/fluency.verify_defects +
# processor.compute_score）。这样「没有错误却给 20 分」在结构上不可能发生。
# --------------------------------------------------------------------------- #
DEFECT_TAXONOMY_VERSION = "en-defect-taxonomy-v1"
DEFECT_MODEL_VERSION = "Qwen3.5-2B-local-fluency-defect-v3"
DEFECT_SCORE_SEMANTICS = "defect_count_v1"
DEFECT_MAX_ITEMS = 3
DEFECT_MAX_NEW_TOKENS = 160

# 类型表只用于写进 prompt 让模型选词；严重度与是否计分由算子侧配置决定，
# 适配器不参与打分。
#
# 2026-11-06 冒烟后的修正：prompt 原先列出全部 10 类，而算子只对 4 类计分，
# 于是模型的 19 条主张里 14 条落在白名单外（article/fragment/collocation…），
# 全部记 suspect、所有记录都拿 100 分——「检测器检不出东西」。另外 20 条冒烟
# 样本取自 14 号业务产物，中位 8 词、67% 不以句末标点结尾，`fragment` 命中的
# 是上游切分留下的残句，不是译文错误。因此 prompt 只保留可机械核验的 4 类，
# 并明确说明输入可能是长文里切出来的一段。
DEFECT_TYPE_GUIDE = """Types (use the exact English word, nothing else):
agreement - subject and verb, or noun and number, do not agree
verb_form - wrong tense, wrong participle, or a missing auxiliary verb
dup_word - the same word is written twice in a row
punctuation - broken punctuation, including several punctuation marks in a row"""

DEFECT_SYSTEM_PROMPT = (
    "You are a grammar error detector for target-language text. You never rate, rank or "
    "score text. You only report concrete errors that you can point to verbatim. "
    "Reply with exactly ONE line of JSON only: no markdown, no explanations."
)

DEFECT_PROMPT = (
    "Find the grammar errors in the text below. Report only errors that are really there.\n"
    "Rules:\n"
    "1. The text may be a piece cut out of a longer text. It can start or end in the middle "
    "of a sentence, and a missing subject, missing object or missing verb at its edges is not "
    "an error. Do not report it.\n"
    "2. Copy the smallest wrong part of the text into span, exactly as it appears. Do not "
    "reword it and do not add quotation marks.\n"
    "3. Write in fix the corrected form of that same span. fix must be different from span "
    "and must change at most two words.\n"
    "4. If the text has no error, return an empty defects list. An empty list is the correct "
    "answer for correct text, and it is the usual answer. Never invent an error to look strict.\n"
    "5. Report at most 3 errors and use each type at most once.\n"
    "6. Do not judge meaning, faithfulness, numbers, terminology, style, wording, word order, "
    "articles, prepositions or length.\n\n"
    + DEFECT_TYPE_GUIDE + "\n\n"
    "Examples:\n"
    "Text: The results was published last year.\n"
    '{"defects":[{"type":"agreement","span":"results was","fix":"results were"}]}\n'
    "Text: Italy is a country in the south of Europe.\n"
    '{"defects":[]}\n'
    "Text: The report was was sent yesterday.\n"
    '{"defects":[{"type":"dup_word","span":"was was","fix":"was"}]}\n'
    "Text: aged 33, 34 and 39,\n"
    '{"defects":[]}\n\n'
    "Format rules (mandatory):\n"
    "1. Output exactly ONE line. Never put a line break inside the JSON.\n"
    "2. Every value must be plain text: do NOT use quotation marks or apostrophes inside any "
    "string value.\n"
    "3. Use an empty list when there is nothing to report; never write none or n/a.\n"
    "4. Do not add any text before or after the JSON object.\n\n"
    "Text to check:\n"
)

DEFECT_SCHEMA_LINE = ('Reply with exactly ONE line of JSON and nothing else: '
                      '{"defects": [{"type": <type>, "span": <exact text>, "fix": <corrected text>}]}')


def _defect_prompt(text: str, target_lang: str) -> str:
    language = (target_lang or "en").strip() or "en"
    clipped = text if len(text) <= MAX_INPUT_CHARS else text[:MAX_INPUT_CHARS]
    return (f"Target language: {language}. " + DEFECT_PROMPT + clipped + "\n"
            + DEFECT_SCHEMA_LINE)


# --------------------------------------------------------------------------- #
# 懒加载：模块 import 不触碰 4.5GB 权重（测试与降级路径只 import 不推理）
# --------------------------------------------------------------------------- #

def _load_model() -> Tuple[Any, Any]:
    global _MODEL, _TOKENIZER
    if _MODEL is not None and _TOKENIZER is not None:
        return _TOKENIZER, _MODEL
    with _LOAD_LOCK:
        if _MODEL is not None and _TOKENIZER is not None:
            return _TOKENIZER, _MODEL
        if not (MODEL_DIR / "config.json").is_file():
            raise RuntimeError("Qwen3.5-2B 本地权重缺失：缺少 config.json")
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                str(MODEL_DIR), local_files_only=True)
        except Exception as exc:  # 依赖/权重问题按结构化失败上抛
            raise RuntimeError("Qwen3.5-2B tokenizer 加载失败：" + type(exc).__name__) from exc
        try:
            model = AutoModelForImageTextToText.from_pretrained(
                str(MODEL_DIR), local_files_only=True, dtype=TORCH_DTYPE,
                low_cpu_mem_usage=True)
        except Exception as exc:
            raise RuntimeError("Qwen3.5-2B 权重加载失败：" + type(exc).__name__) from exc
        model = model.to(DEVICE)
        model.eval()
        _TOKENIZER, _MODEL = tokenizer, model
        return _TOKENIZER, _MODEL


# --------------------------------------------------------------------------- #
# 严格 JSON 解析（只接受模型真实输出；失败抛异常，绝不补默认分）
# --------------------------------------------------------------------------- #

def _balanced_object(text: str) -> Optional[str]:
    """提取第一个括号平衡的 JSON 对象（模型偶尔带前言/代码围栏）。"""
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


def _parse_payload(raw: str) -> Dict[str, Any]:
    """把模型文本解析为评分对象；任何不合法都抛 RuntimeError。"""
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("Qwen3.5-2B 未生成任何文本")
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    payload: Any = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        candidate = _balanced_object(text)
        if candidate is None:
            raise RuntimeError("Qwen3.5-2B 响应不是有效 JSON")
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Qwen3.5-2B 响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Qwen3.5-2B 响应 JSON 不是对象")

    score = payload.get("fluency_score")
    if score is None:
        # [2026-11-04 v2] 模型偶发把键写成 score：只接受这一个等价键，仍然要求数值。
        score = payload.get("score")
    if isinstance(score, str):
        # [2026-11-04 v2] 模型偶发把分数写成 "87"：能无损转成数字就接受，
        # 这不是补默认分（分数仍来自模型真实输出），但绝不做四舍五入或映射。
        try:
            score = float(score.strip())
        except ValueError:
            raise RuntimeError("Qwen3.5-2B 未返回数值型 fluency_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise RuntimeError("Qwen3.5-2B 未返回数值型 fluency_score")
    score = float(score)
    if not (SCORE_MIN <= score <= SCORE_MAX):
        raise RuntimeError(f"Qwen3.5-2B fluency_score={score} 超出 [0, 100]")

    def _string_list(name: str) -> List[Any]:
        value = payload.get(name)
        if value is None:
            return []
        if isinstance(value, str):
            cleaned = [value] if value.strip() and value.strip().lower() not in {"none", "无", "n/a", "null"} else []
            return cleaned
        if not isinstance(value, list):
            raise RuntimeError(f"Qwen3.5-2B {name} 不是列表")
        return value

    grammar_errors = [item for item in _string_list("grammar_errors")
                      if isinstance(item, (str, dict))]
    raw_evidence = [item for item in _string_list("evidence")
                    if isinstance(item, (str, dict))]

    evidence: List[Dict[str, Any]] = []
    for item in raw_evidence[:3]:
        if isinstance(item, dict):
            detail = item.get("detail") or item.get("type") or json.dumps(item, ensure_ascii=False)
            evidence.append({"type": "model_evidence", "detail": str(detail)[:300]})
        else:
            evidence.append({"type": "model_evidence", "detail": item[:300]})
    evidence.append({
        "type": "model_rubric",
        "detail": f"{MODEL_ID} 直接给出 0-100 rubric 分（未经 Gold 校准曲线）",
    })
    return {
        "fluency_score": score,
        "grammar_errors": grammar_errors,
        "evidence": evidence,
        "model_version": MODEL_VERSION,
        "score_semantics": SCORE_SEMANTICS,
        "business_gate_approved": BUSINESS_GATE_APPROVED,
        "rubric_version": RUBRIC_VERSION,
        "device": DEVICE,
    }


# --------------------------------------------------------------------------- #
# 推理
# --------------------------------------------------------------------------- #

RETRY_TEMPERATURE = 0.2
RETRY_TOP_P = 0.9


def _messages(text: str, target_lang: str, prompt_fn=None, system: str = "") -> List[dict]:
    """组装 chat 消息；prompt_fn/system 缺省为 rubric-v2 口径，缺陷模式显式传入。"""
    clipped = text if len(text) <= MAX_INPUT_CHARS else text[:MAX_INPUT_CHARS]
    return [{"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": (prompt_fn or _user_prompt)(clipped, target_lang)}]


def _encode_prompt(tokenizer, text: str, target_lang: str, prompt_fn=None,
                   system: str = "") -> Dict[str, Any]:
    """把一条文本编成 chat 提示（返回 dict[str, list[int]]，供批量左填充使用）。"""
    messages = _messages(text, target_lang, prompt_fn, system)
    try:
        return tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors=None,
            return_dict=True, enable_thinking=False)
    except TypeError:  # 兼容不支持 enable_thinking 的模板
        return tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors=None,
            return_dict=True)


def _generate_batch(texts: List[str], target_lang: str, prompt_fn=None,
                    system: str = "", max_new_tokens: int = MAX_NEW_TOKENS) -> List[str]:
    """一次前向解码一批提示；decoder-only 模型用左填充保证新 token 对齐。

    前提：所有提示共享同一段 rubric/系统提示，长度接近，填充浪费很小。
    任一环节失败（OOM、模板不支持 batch）由调用方整批回退逐条。
    """
    tokenizer, model = _load_model()
    encoded = [_encode_prompt(tokenizer, text, target_lang, prompt_fn, system) for text in texts]
    previous_side = getattr(tokenizer, "padding_side", "right")
    tokenizer.padding_side = "left"
    try:
        batch = tokenizer.pad(encoded, padding=True, return_tensors="pt")
    finally:
        tokenizer.padding_side = previous_side
    batch = {name: value.to(DEVICE) for name, value in batch.items()}
    input_len = batch["input_ids"].shape[-1]
    pad_token_id = getattr(tokenizer, "pad_token_id", None) \
        or getattr(tokenizer, "eos_token_id", None)
    options: Dict[str, Any] = {"max_new_tokens": max_new_tokens, "num_beams": 1,
                               "pad_token_id": pad_token_id, "do_sample": False}
    with torch.inference_mode():
        generated = model.generate(**batch, **options)
    return [tokenizer.decode(row[input_len:], skip_special_tokens=True).strip()
            for row in generated]


def _generate(text: str, target_lang: str, sample: bool = False, prompt_fn=None,
              system: str = "", max_new_tokens: int = MAX_NEW_TOKENS) -> str:
    """生成一次评分文本；sample=True 用于解析失败后的唯一一次重试（贪心解码会复现同一畸形）。"""
    tokenizer, model = _load_model()
    messages = _messages(text, target_lang, prompt_fn, system)
    try:
        inputs = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt",
            return_dict=True, enable_thinking=False)
    except TypeError:  # 兼容不支持 enable_thinking 的模板
        inputs = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
    inputs = {name: value.to(DEVICE) for name, value in inputs.items()}
    input_len = inputs["input_ids"].shape[-1]
    pad_token_id = getattr(tokenizer, "pad_token_id", None) \
        or getattr(tokenizer, "eos_token_id", None)
    options: Dict[str, Any] = {"max_new_tokens": max_new_tokens, "num_beams": 1,
                               "pad_token_id": pad_token_id}
    if sample:
        options.update({"do_sample": True, "temperature": RETRY_TEMPERATURE,
                        "top_p": RETRY_TOP_P})
    else:
        options["do_sample"] = False
    with torch.inference_mode():
        generated = model.generate(**inputs, **options)
    return tokenizer.decode(generated[0][input_len:], skip_special_tokens=True).strip()


def score_fluency(text: str, target_lang: str = "en") -> Dict[str, Any]:
    """单条流畅度打分；文本非法/解析失败/分数越界一律抛异常。

    [2026-11-02 B2] 解析失败时用 temperature=0.2 重试一次，成功则标
    model_output_repaired=True（分数是重试那次的真实输出，不是修出来的）。
    仍失败保持异常上抛，由上层记 model_error——绝不补默认分。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("待评分译文为空或不是字符串")
    try:
        payload = _parse_payload(_generate(text, target_lang))
        payload["model_output_repaired"] = False
        return payload
    except RuntimeError as first_error:
        try:
            payload = _parse_payload(_generate(text, target_lang, sample=True))
        except RuntimeError:
            raise first_error
        payload["model_output_repaired"] = True
        return payload


def _score_one_from_raw(text: str, target_lang: str, raw: str) -> Dict[str, Any]:
    """解析批量解码结果；解析失败仍按单条路径重试一次（temperature=0.2）。"""
    try:
        payload = _parse_payload(raw)
        payload["model_output_repaired"] = False
        return payload
    except RuntimeError as first_error:
        try:
            payload = _parse_payload(_generate(text, target_lang, sample=True))
        except RuntimeError:
            raise first_error
        payload["model_output_repaired"] = True
        return payload


def _fast_batched_kernels_available() -> bool:
    """批量解码是否值得开：取决于是否装了快速卷积/线性注意力核。

    2026-11-05 实测（RTX 4060 Laptop，未装 causal_conv1d / flash-linear-attention）：
    批量 4 条解码 8 条耗时 320s（40s/条），逐条 1.8s/条——参考实现下批量慢 20 倍，
    因为每批都要把提示左填充到最长一条，而混合线性注意力层在参考实现里是逐位置
    串行扫描的。所以只在装了快速核时才走批量，否则明确回退逐条。
    """
    for module_name in ("causal_conv1d", "fla"):
        try:
            __import__(module_name)
        except Exception:
            return False
    return True


def score_fluency_batch(texts: List[str], target_lang: str = "en") -> List[Any]:
    """批量打分：先确保权重可加载（整批级故障上抛），再按 BATCH_SIZE 分批解码。

    每批一次 generate；批级失败（OOM/模板不支持）整批回退逐条，
    单条解析失败仍走既有的一次采样重试。绝不伪造分数。
    没装快速核时批量反而更慢，直接逐条（见 _fast_batched_kernels_available）。
    """
    texts = list(texts)
    _load_model()
    outputs: List[Any] = [None] * len(texts)
    pending: List[Tuple[int, str]] = []
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            # 与单条入口保持一致：空文本在生成之前就判错
            outputs[index] = ValueError("待评分译文为空或不是字符串")
        else:
            pending.append((index, text))
    if BATCH_SIZE <= 1 or not _fast_batched_kernels_available():
        for index, text in pending:
            try:
                outputs[index] = score_fluency(text, target_lang)
            except Exception as exc:  # 单条失败按元素透传，不吞错、不伪造分数
                outputs[index] = exc
        return outputs
    for start in range(0, len(pending), BATCH_SIZE):
        chunk = pending[start:start + BATCH_SIZE]
        try:
            raws = _generate_batch([text for _, text in chunk], target_lang)
        except Exception:
            raws = None
        if raws is None:
            for index, text in chunk:
                try:
                    outputs[index] = score_fluency(text, target_lang)
                except Exception as exc:  # 单条失败按元素透传，不吞错、不伪造分数
                    outputs[index] = exc
            continue
        for (index, text), raw in zip(chunk, raws):
            try:
                outputs[index] = _score_one_from_raw(text, target_lang, raw)
            except Exception as exc:
                outputs[index] = exc
    return outputs


# --------------------------------------------------------------------------- #
# defect_count_v1：缺陷主张提取（不打分）
#
# 契约（被 PRLt_fluency_check/utils/fluency.py 的 LocalModelScorer 调用）：
#   - detect_defects(text, target_lang) -> dict
#       {defects:[{type, span, fix}], model_version, score_semantics,
#        business_gate_approved, taxonomy_version, model_output_repaired, device}
#   - detect_defects_batch(texts, target_lang) -> list[dict | Exception]
# 这里只做「提出主张」，**不做核验、不打分**：span 是否真在原文里、fix 是否最小修正、
# 该类型是否可信，全部由算子侧 utils/fluency.verify_defects 与 processor.compute_score
# 完成。模型不返回任何分数，因此不存在「先给分再编理由」的空间。
# --------------------------------------------------------------------------- #

KNOWN_DEFECT_TYPES = {
    "agreement", "verb_form", "dup_word", "punctuation", "fragment",
    "article", "preposition", "collocation", "word_order", "other",
}


def _clean_fragment(value: Any) -> str:
    """模型常在 span/fix 外面套引号或反引号；去掉首尾引号与多余空白。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    for quote in ('"', "'", "`", "“", "”", "‘", "’"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
    return re.sub(r"\s+", " ", text)


def _parse_defect_payload(raw: str) -> List[Dict[str, str]]:
    """把模型输出解析为缺陷主张列表；结构不合法一律抛 RuntimeError。

    注意：单条主张内部的 span/fix 是否成立**不在这里判断**（那属于核验层），
    这里只保证「拿到的是一组结构完整的候选」。空列表是合法且期望的答案。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("Qwen3.5-2B 未生成任何文本")
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    payload: Any = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        candidate = _balanced_object(text)
        if candidate is None:
            raise RuntimeError("Qwen3.5-2B 响应不是有效 JSON")
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Qwen3.5-2B 响应不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Qwen3.5-2B 响应 JSON 不是对象")
    items = payload.get("defects")
    if items is None:
        # 只容忍一个等价键；不接受「用 grammar_errors 顶替」——那是 rubric 口径的字段，
        # 没有 span/fix，无法核验，混用会让下游误以为主张已被定位。
        items = payload.get("errors")
    if items is None:
        raise RuntimeError("Qwen3.5-2B 未返回 defects 列表")
    if not isinstance(items, list):
        raise RuntimeError("Qwen3.5-2B defects 不是列表")

    defects: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        defect_type = str(item.get("type") or "other").strip().lower().replace(" ", "_")
        if defect_type not in KNOWN_DEFECT_TYPES:
            defect_type = "other"
        span = _clean_fragment(item.get("span"))
        fix = _clean_fragment(item.get("fix"))
        if not span:
            continue
        key = (defect_type, span.lower())
        if key in seen:
            continue
        seen.add(key)
        defects.append({"type": defect_type, "span": span, "fix": fix})
        if len(defects) >= DEFECT_MAX_ITEMS:
            break
    return defects


def _defect_result(defects: List[Dict[str, str]], repaired: bool) -> Dict[str, Any]:
    return {
        "defects": defects,
        "model_version": DEFECT_MODEL_VERSION,
        "score_semantics": DEFECT_SCORE_SEMANTICS,
        "business_gate_approved": BUSINESS_GATE_APPROVED,
        "taxonomy_version": DEFECT_TAXONOMY_VERSION,
        "model_output_repaired": repaired,
        "device": DEVICE,
    }


def detect_defects(text: str, target_lang: str = "en") -> Dict[str, Any]:
    """提取缺陷主张；解析失败时用 temperature=0.2 重试一次，仍失败则抛异常。

    与 score_fluency 一致：绝不补默认值。空列表是合法结果（表示「没找到错误」），
    与「解析失败」严格区分——前者返回 defects=[]，后者抛 RuntimeError 由上层记 model_error。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("待检测译文为空或不是字符串")
    kwargs = {"prompt_fn": _defect_prompt, "system": DEFECT_SYSTEM_PROMPT,
              "max_new_tokens": DEFECT_MAX_NEW_TOKENS}
    try:
        return _defect_result(_parse_defect_payload(_generate(text, target_lang, **kwargs)), False)
    except RuntimeError as first_error:
        try:
            defects = _parse_defect_payload(
                _generate(text, target_lang, sample=True, **kwargs))
        except RuntimeError:
            raise first_error
        return _defect_result(defects, True)


def _detect_one_from_raw(text: str, target_lang: str, raw: str) -> Dict[str, Any]:
    kwargs = {"prompt_fn": _defect_prompt, "system": DEFECT_SYSTEM_PROMPT,
              "max_new_tokens": DEFECT_MAX_NEW_TOKENS}
    try:
        return _defect_result(_parse_defect_payload(raw), False)
    except RuntimeError as first_error:
        try:
            defects = _parse_defect_payload(
                _generate(text, target_lang, sample=True, **kwargs))
        except RuntimeError:
            raise first_error
        return _defect_result(defects, True)


def detect_defects_batch(texts: List[str], target_lang: str = "en") -> List[Any]:
    """批量缺陷提取；批量路径不可用时逐条，单条失败按元素返回 Exception。"""
    texts = list(texts)
    _load_model()
    outputs: List[Any] = [None] * len(texts)
    pending: List[Tuple[int, str]] = []
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            outputs[index] = ValueError("待检测译文为空或不是字符串")
        else:
            pending.append((index, text))
    if BATCH_SIZE <= 1 or not _fast_batched_kernels_available():
        for index, text in pending:
            try:
                outputs[index] = detect_defects(text, target_lang)
            except Exception as exc:
                outputs[index] = exc
        return outputs
    for start in range(0, len(pending), BATCH_SIZE):
        chunk = pending[start:start + BATCH_SIZE]
        try:
            raws = _generate_batch([text for _, text in chunk], target_lang,
                                   prompt_fn=_defect_prompt, system=DEFECT_SYSTEM_PROMPT,
                                   max_new_tokens=DEFECT_MAX_NEW_TOKENS)
        except Exception:
            raws = None
        if raws is None:
            for index, text in chunk:
                try:
                    outputs[index] = detect_defects(text, target_lang)
                except Exception as exc:
                    outputs[index] = exc
            continue
        for (index, text), raw in zip(chunk, raws):
            try:
                outputs[index] = _detect_one_from_raw(text, target_lang, raw)
            except Exception as exc:
                outputs[index] = exc
    return outputs
