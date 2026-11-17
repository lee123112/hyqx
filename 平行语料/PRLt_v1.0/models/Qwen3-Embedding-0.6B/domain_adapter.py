#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""算子16（交付编号 11）领域识别适配器 —— Qwen3-Embedding-0.6B 零样本原型分类。

===========================================================================
诚实标注（必读）
===========================================================================
1. 本适配器使用本地 ``models/Qwen3-Embedding-0.6B`` 权重（1024 维），分类策略是
   **零样本原型匹配（zero-shot prototype）**：先对 7 个标签
   （medical/legal/finance/technology/education/out_of_scope/unknown）的种子描述
   做平均嵌入得到原型，再用"句对嵌入 与 原型"的余弦相似度经温度 softmax 得到
   0–1 的真实概率。它**不是**人工标注训练的监督分类器：
   ``training_label_quality="no_human_item_level_domain_gold"``、
   ``formal_business_accuracy="blocked_no_human_item_level_domain_gold"``。
2. 证据里 ``method="zero_shot_prototype"``。本适配器**没有**复用任何 BGE-M3
   两阶段逻辑回归系数，也没有沿用其冻结分类器；旧 ``models/bge-domain-prototype``
   保持不动但已不被配置引用。
3. 业务门禁仍是 ``waiting_manual``：模型给出的概率只是筛选证据，不单独放行；
   0.80 阈值由算子配置 ``domain_confidence_threshold`` 施加，适配器只输出真实概率，
   在概率不足时如实返回低置信，绝不伪造置信度或补足到阈值以上。

接口（与冻结适配器一致，供 ``utils/domain.py::LocalDomainClassifier`` 调用）
----------------------------------------------------------------------------
``classify_domain(source, target, language_pair="zh-en") -> dict``，返回
``domain`` / ``subdomain`` / ``domain_confidence``(0–1) / ``candidates`` /
``evidence`` / ``model_version``。

* ``domain`` ∈ 五个允许领域 ∪ ``{out_of_scope, unknown}``。
* ``domain_confidence`` = 温度 softmax 中该标签的概率（真实计算，未校准到人工 Gold）。
* ``candidates`` 按概率降序给出全部标签（供 allow_multi_label 使用）。

模型路径解析：``QWEN3_EMBEDDING_MODEL_PATH`` 环境变量 → 本适配器所在目录
（即 ``models/Qwen3-Embedding-0.6B``）→ ``MODEL_ROOT/Qwen3-Embedding-0.6B``。
设备可用 ``QWEN3_DEVICE`` 覆盖（默认 cpu）；全程离线，不下载权重。

自测（真实推理，不联网）::

    G:\\HYQX\\算子\\_env\\prlt_clean\\Scripts\\python.exe -X utf8 \
        models/Qwen3-Embedding-0.6B/domain_adapter.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from functools import lru_cache
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PROTOTYPE_PATH = Path(os.environ.get(
    "QWEN3_DOMAIN_PROTOTYPE_PATH", str(HERE / "domain_prototypes.npz")))
DEVICE = os.environ.get("QWEN3_DEVICE", "cpu")

MODEL_ID = "Qwen3-Embedding-0.6B"
MODEL_VERSION = "Qwen3-Embedding-0.6B-zero-shot-prototype-v1.0"
EMBEDDING_DIMENSION = 1024
MAX_LENGTH = 512
# 温度：把余弦差放大成可区分的概率分布。0.03 是开发期在 6 条自测样例 + 5 条额外
# 边界样例（体育/娱乐/日常/跨领域）+ 算子 demo 上人工检查后选定的固定超参数：
# 清晰领域样例超过算子 0.80 阈值，含糊/跨领域样例仍留在阈值以下。没有人工 Gold，
# 这不是准确率校准，只是让冻结阈值可用的工程取值（报告里有 T=0.03/0.04/0.05 对照）。
TEMPERATURE = 0.03
ACCEPTANCE_THRESHOLD = 0.80

TRAINING_LABEL_QUALITY = "no_human_item_level_domain_gold"
FORMAL_BUSINESS_ACCURACY = "blocked_no_human_item_level_domain_gold"
BUSINESS_GATE = "waiting_manual"
BUSINESS_GATE_APPROVED = False

DOMAIN_LABELS = (
    "medical", "legal", "finance", "technology", "education",
    "out_of_scope", "unknown",
)

# 种子描述：每个标签 3 条（中英各一 + 一条场景句），刻意写成通用领域描述，
# 不复用任何评测句对，避免"种子就是测试样本"的泄漏。
SEED_TEXTS = {
    "medical": [
        "医学与医疗健康：患者、疾病、诊断、治疗、药物、手术、医院、临床研究。",
        "Medical and healthcare content about patients, diseases, diagnosis, treatment, drugs, "
        "surgery and hospitals.",
        "临床医学研究讨论症状、检查结果与治疗方案。",
    ],
    "legal": [
        "法律与司法事务：法规、合同、诉讼、法院、律师、判决、赔偿、权利义务。",
        "Legal content about laws, regulations, contracts, litigation, courts, lawyers, "
        "judgments and liabilities.",
        "司法程序依据法律条文认定责任并作出裁判。",
    ],
    "finance": [
        "金融与经济：银行、利率、投资、股票、债券、贷款、货币、经济增长。",
        "Finance and economics content about banks, interest rates, investments, stocks, loans "
        "and economic growth.",
        "金融市场分析资产价格、融资成本与投资收益。",
    ],
    "technology": [
        "计算机与技术：软件、硬件、算法、数据库、网络、人工智能、编程、系统架构。",
        "Technology and computing content about software, hardware, algorithms, databases, "
        "networks and artificial intelligence.",
        "工程师设计系统架构并优化算法与数据处理流程。",
    ],
    "education": [
        "教育与教学：学校、课程、教师、学生、课堂、考试、学业、培训。",
        "Education content about schools, courses, teachers, students, classrooms, examinations "
        "and training.",
        "教学活动中教师安排课程并评估学生的学习成果。",
    ],
    "out_of_scope": [
        "体育、娱乐与日常生活：比赛、运动员、明星、影视、音乐、天气、旅游、美食。",
        "Sports, entertainment and everyday life: matches, athletes, celebrities, films, music, "
        "weather and travel.",
        "休闲话题讲述生活见闻、旅游经历与兴趣爱好。",
    ],
    "unknown": [
        "无法判断所属领域的文本，内容含糊、过短或没有明确主题。",
        "Text whose domain cannot be determined because it is ambiguous, too short or has no "
        "clear topic.",
        "仅有零散词语或无法归类的片段。",
    ],
}

if set(SEED_TEXTS) != set(DOMAIN_LABELS):
    raise ValueError("种子描述标签集合与 DOMAIN_LABELS 不一致")


# --------------------------------------------------------------------------- #
# 关键词相关性（2026-11-03 需求 30 号：领域关键词 + 相关性阈值）
# --------------------------------------------------------------------------- #
# 负样本：与业务领域无关的通用/日常/新闻文本。打分时取"关键词最高余弦"与
# "负样本最高余弦"的差值再过 logistic，0.5 表示打平。负样本只是让分值有一个
# 相对参照，不代表任何人工标注。
RELEVANCE_NEGATIVE_TEXTS = (
    "新闻报道：交通事故、警方行动、社会治安与地方事件。",
    "News reports about traffic accidents, police activity, crime and local events.",
    "日常生活与休闲：天气、旅游、美食、家庭聚会和朋友。",
    "Everyday life and leisure: weather, travel, food, family gatherings and friends.",
    "体育赛事与娱乐新闻：比赛结果、运动员、明星和影视作品。",
    "Sports results and entertainment news about athletes, celebrities and films.",
    "个人经历与情感分享，讲述生活中的所见所闻。",
    "Personal stories and emotional reflections about everyday experiences.",
    "简短的事实陈述，没有明确的专业主题。",
    "A brief factual statement with no clear professional subject.",
)

RELEVANCE_SCORE_TYPE = "logistic_cos_diff"
RELEVANCE_CALIBRATION = "no_human_relevance_gold"
RELEVANCE_TEMPERATURE = 0.05
RELEVANCE_AGGREGATES = ("max", "top3_mean")

_KEYWORD_VECTORS: dict[str, np.ndarray] = {}
_NEGATIVE_VECTORS: np.ndarray | None = None


def _seed_fingerprint() -> str:
    payload = json.dumps(
        {"labels": list(DOMAIN_LABELS), "seeds": SEED_TEXTS},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


SEED_FINGERPRINT = _seed_fingerprint()


# --------------------------------------------------------------------------- #
# 编码器
# --------------------------------------------------------------------------- #
def _model_candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("QWEN3_EMBEDDING_MODEL_PATH")
    if env:
        candidates.append(Path(env))
    candidates.append(HERE)
    model_root = os.environ.get("MODEL_ROOT")
    if model_root:
        candidates.append(Path(model_root) / MODEL_ID)
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        if (path / "config.json").is_file():
            unique.append(path)
    return unique


@lru_cache(maxsize=1)
def _resolve_encoder() -> Path:
    candidates = _model_candidates()
    if not candidates:
        raise FileNotFoundError(
            "未找到本地 Qwen3-Embedding-0.6B 权重目录（需要 config.json）："
            + "、".join(str(path) for path in [HERE]))
    return candidates[0]


@lru_cache(maxsize=1)
def _encoder():
    """加载 tokenizer + 模型；Qwen3 embedding 用左填充 + 末位 token 池化。"""
    import torch
    from transformers import AutoModel, AutoTokenizer

    source = _resolve_encoder()
    tokenizer = AutoTokenizer.from_pretrained(str(source), padding_side="left")
    model = AutoModel.from_pretrained(str(source), dtype=torch.float32)
    model.eval()
    if DEVICE != "cpu":
        model.to(DEVICE)
    hidden = int(getattr(model.config, "hidden_size", 0))
    if hidden != EMBEDDING_DIMENSION:
        raise ValueError(f"{MODEL_ID} 隐藏维度异常：{hidden} != {EMBEDDING_DIMENSION}")
    return tokenizer, model, torch, source


def encode(texts: list[str]) -> np.ndarray:
    """批量编码为 L2 归一化向量；非法输出抛 ValueError（不返回假向量）。"""
    tokenizer, model, torch, _source = _encoder()
    batch = [str(text) if text is not None else "" for text in texts]
    if not batch:
        return np.zeros((0, EMBEDDING_DIMENSION), dtype=np.float32)
    encoded = tokenizer(batch, padding=True, truncation=True, max_length=MAX_LENGTH,
                        return_tensors="pt")
    if DEVICE != "cpu":
        encoded = {key: value.to(DEVICE) for key, value in encoded.items()}
    with torch.no_grad():
        hidden = model(**encoded).last_hidden_state
    # 左填充：最后一个位置即最后一条真实 token。
    last = hidden[:, -1]
    vectors = torch.nn.functional.normalize(last, p=2, dim=1).cpu().numpy().astype(np.float32)
    if vectors.shape != (len(batch), EMBEDDING_DIMENSION) or not np.isfinite(vectors).all():
        raise ValueError("Qwen3-Embedding 返回了非法嵌入")
    return vectors


# --------------------------------------------------------------------------- #
# 零样本原型
# --------------------------------------------------------------------------- #
def _build_prototypes() -> dict:
    labels = list(DOMAIN_LABELS)
    flat = [text for label in labels for text in SEED_TEXTS[label]]
    seed_vectors = encode(flat)
    offset = 0
    vectors = []
    for label in labels:
        count = len(SEED_TEXTS[label])
        block = seed_vectors[offset:offset + count]
        offset += count
        mean = block.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if not np.isfinite(norm) or norm <= 0.0:
            raise ValueError(f"标签 {label} 的原型向量非法")
        vectors.append((mean / norm).astype(np.float32))
    matrix = np.stack(vectors)
    payload = {
        "labels": np.array(labels),
        "vectors": matrix,
        "seed_fingerprint": np.array(SEED_FINGERPRINT),
        "model_id": np.array(MODEL_ID),
        "embedding_dimension": np.array(EMBEDDING_DIMENSION, dtype=np.int64),
    }
    return payload


def _read_cached_prototypes() -> dict | None:
    if not PROTOTYPE_PATH.is_file():
        return None
    try:
        with np.load(PROTOTYPE_PATH, allow_pickle=False) as artifact:
            payload = {key: artifact[key] for key in artifact.files}
    except (OSError, ValueError):
        return None
    labels = tuple(str(value) for value in payload.get("labels", []).tolist())
    fingerprint = str(payload.get("seed_fingerprint", "")) if "seed_fingerprint" in payload else ""
    vectors = payload.get("vectors")
    if labels != DOMAIN_LABELS or fingerprint != SEED_FINGERPRINT:
        return None
    if vectors is None or vectors.shape != (len(DOMAIN_LABELS), EMBEDDING_DIMENSION):
        return None
    if not np.isfinite(vectors).all():
        return None
    return payload


@lru_cache(maxsize=1)
def _prototypes() -> dict:
    cached = _read_cached_prototypes()
    if cached is not None:
        return cached
    built = _build_prototypes()
    try:
        np.savez(PROTOTYPE_PATH, **built)
    except OSError:
        # 只读目录：仍用内存中的原型完成本次推理，不伪造结果。
        pass
    return built


# --------------------------------------------------------------------------- #
# 推理
# --------------------------------------------------------------------------- #
def _softmax(logits: np.ndarray, temperature: float) -> np.ndarray:
    scaled = np.asarray(logits, dtype=np.float64) / float(temperature)
    scaled = scaled - float(np.max(scaled))
    weights = np.exp(scaled)
    return weights / float(np.sum(weights))


def classify_domain(source: str, target: str, language_pair: str = "zh-en") -> dict:
    """零样本原型领域分类；返回结构与冻结适配器一致，并带如实的零样本溯源证据。"""
    if not isinstance(source, str) or not isinstance(target, str):
        raise ValueError("source/target 必须是字符串")
    if not source.strip() and not target.strip():
        raise ValueError("source/target 不能同时为空")

    payload = _prototypes()
    labels = [str(value) for value in payload["labels"].tolist()]
    matrix = payload["vectors"]
    vector = encode([f"{source}\n{target}"])[0]

    cosines = matrix @ vector
    probabilities = _softmax(cosines, TEMPERATURE)
    order = np.argsort(-probabilities)
    predicted_index = int(order[0])
    domain = labels[predicted_index]
    confidence = float(probabilities[predicted_index])

    candidates = [
        {
            "domain": labels[int(index)],
            "subdomain": None,
            "confidence": float(probabilities[int(index)]),
        }
        for index in order
    ]

    tokenizer, _model, _torch, source_dir = _encoder()
    evidence = [{
        "type": "zero_shot_prototype_decision",
        "method": "zero_shot_prototype",
        "method_description": (
            "句对嵌入与 7 个标签种子描述平均嵌入（原型）的余弦相似度，"
            "经温度 softmax 得到标签概率；无监督训练、无人工逐条 Gold。"
        ),
        "encoder": MODEL_ID,
        "embedding_dimension": EMBEDDING_DIMENSION,
        "pooling": "last_token_left_padding",
        "max_length": MAX_LENGTH,
        "language_pair": language_pair,
        "predicted_domain": domain,
        "cosine_similarities": {
            label: float(cosines[index]) for index, label in enumerate(labels)
        },
        "probabilities": {
            label: float(probabilities[index]) for index, label in enumerate(labels)
        },
        "temperature": TEMPERATURE,
        "domain_confidence_semantics": "temperature_softmax_cosine_to_seed_prototypes",
        "acceptance_threshold": ACCEPTANCE_THRESHOLD,
        "acceptance_threshold_enforced_by": "operator_config.domain_confidence_threshold",
        "training_label_quality": TRAINING_LABEL_QUALITY,
        "formal_business_accuracy": FORMAL_BUSINESS_ACCURACY,
        "business_gate": BUSINESS_GATE,
        "business_gate_approved": BUSINESS_GATE_APPROVED,
        "prototype_source": "seed_description_mean_embedding",
        "prototype_labels": list(labels),
        "prototype_seed_fingerprint": SEED_FINGERPRINT,
        "prototype_file": PROTOTYPE_PATH.name,
        "legacy_bge_logistic_reused": False,
        "encoder_path": str(source_dir),
        "device": DEVICE,
    }]
    return {
        "domain": domain,
        "subdomain": None,
        "domain_confidence": confidence,
        "candidates": candidates,
        "evidence": evidence,
        "model_version": f"{MODEL_VERSION}(prototype_seed_sha256={SEED_FINGERPRINT[:12]})",
    }


# --------------------------------------------------------------------------- #
# 关键词相关性打分
# --------------------------------------------------------------------------- #
def _keyword_matrix(keywords: list[str]) -> np.ndarray:
    """关键词向量矩阵；同一进程内每个关键词只编码一次（按原样字符串缓存）。"""
    missing = [keyword for keyword in dict.fromkeys(keywords) if keyword not in _KEYWORD_VECTORS]
    if missing:
        vectors = encode(missing)
        for keyword, vector in zip(missing, vectors):
            _KEYWORD_VECTORS[keyword] = vector
    return np.stack([_KEYWORD_VECTORS[keyword] for keyword in keywords])


def _negative_matrix() -> np.ndarray:
    global _NEGATIVE_VECTORS
    if _NEGATIVE_VECTORS is None:
        _NEGATIVE_VECTORS = encode(list(RELEVANCE_NEGATIVE_TEXTS))
    return _NEGATIVE_VECTORS


def _sigmoid(value: float) -> float:
    value = float(value)
    if value >= 0.0:
        return 1.0 / (1.0 + np.exp(-value))
    exp_value = np.exp(value)
    return float(exp_value / (1.0 + exp_value))


def score_relevance(source: str, target: str, keywords, language_pair: str = "zh-en",
                    temperature: float = RELEVANCE_TEMPERATURE,
                    aggregate: str = "max", top_k: int = 3) -> dict:
    """关键词相关性打分：与业务关键词的相似度对比通用文本基线。

    ``score = sigmoid((cos_pos - cos_neg) / temperature)``，0.5 表示"更像通用文本"
    与"更像关键词主题"打平。**这不是概率，也没有人工相关性 Gold**；阈值由算子配置
    ``relevance_threshold`` 施加，适配器只如实给出分值。
    """
    if not isinstance(source, str) or not isinstance(target, str):
        raise ValueError("source/target 必须是字符串")
    if not source.strip() and not target.strip():
        raise ValueError("source/target 不能同时为空")
    if isinstance(keywords, str) or not isinstance(keywords, (list, tuple)):
        raise ValueError("keywords 必须是关键词列表")
    keywords = [str(keyword) for keyword in keywords if str(keyword).strip()]
    if not keywords:
        raise ValueError("keywords 不能为空")
    if aggregate not in RELEVANCE_AGGREGATES:
        raise ValueError("aggregate 只能是 " + "/".join(RELEVANCE_AGGREGATES))
    temperature = float(temperature)
    if not 0.0 < temperature <= 1.0:
        raise ValueError("temperature 必须在 (0, 1] 内")

    vector = encode([f"{source}\n{target}"])[0]
    cosines = _keyword_matrix(keywords) @ vector
    order = np.argsort(-cosines, kind="stable")

    if aggregate == "max":
        cos_pos = float(cosines[int(order[0])])
    else:  # top3_mean：短句/数字句的缓解口径
        cos_pos = float(np.mean(cosines[order[: min(3, len(keywords))]]))
    negative_cosines = _negative_matrix() @ vector
    cos_neg = float(np.max(negative_cosines))
    score = _sigmoid((cos_pos - cos_neg) / temperature)

    matched = [
        {"keyword": keywords[int(index)], "cosine": float(cosines[int(index)])}
        for index in order[: max(1, min(int(top_k), len(keywords)))]
    ]
    evidence = [{
        "type": "keyword_relevance_decision",
        "method": "keyword_relevance_logistic_cos_diff",
        "method_description": (
            "句对嵌入与任务关键词嵌入的余弦（取最大/前 3 均值）减去与内置通用文本"
            "负样本的最大余弦，差值经 logistic 得到 0–1 分值；无人工相关性 Gold。"
        ),
        "encoder": MODEL_ID,
        "embedding_dimension": EMBEDDING_DIMENSION,
        "pooling": "last_token_left_padding",
        "language_pair": language_pair,
        "keyword_count": len(keywords),
        "aggregate": aggregate,
        "temperature": temperature,
        "cos_pos": cos_pos,
        "cos_neg": cos_neg,
        "score_type": RELEVANCE_SCORE_TYPE,
        "calibration": RELEVANCE_CALIBRATION,
        "negative_sample_count": len(RELEVANCE_NEGATIVE_TEXTS),
        "negative_sample_sha256": hashlib.sha256(
            json.dumps(list(RELEVANCE_NEGATIVE_TEXTS), ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "matched_keywords": matched,
    }]
    return {
        "relevance_score": float(score),
        "cos_pos": cos_pos,
        "cos_neg": cos_neg,
        "temperature": temperature,
        "aggregate": aggregate,
        "matched_keywords": matched,
        "relevance_score_type": RELEVANCE_SCORE_TYPE,
        "relevance_calibration": RELEVANCE_CALIBRATION,
        "relevance_backend": "model",
        "keyword_count": len(keywords),
        "evidence": evidence,
        "model_version": f"{MODEL_VERSION}(relevance={RELEVANCE_SCORE_TYPE})",
    }


# --------------------------------------------------------------------------- #
# 自测：6 条真实样例（医疗/法律/金融/技术/教育/通用 各 1 条）
# --------------------------------------------------------------------------- #
SELF_TEST_SAMPLES = [
    ("zh-en", "患者出现持续性胸痛，需要立即检查心脏功能。",
     "The patient has persistent chest pain and needs an immediate cardiac assessment.", "medical"),
    ("zh-en", "法院判决被告赔偿原告的全部损失。",
     "The court ruled that the defendant should compensate the plaintiff for all losses.", "legal"),
    ("zh-en", "央行宣布下调基准利率以刺激经济增长。",
     "The central bank announced a cut in the benchmark interest rate to stimulate growth.", "finance"),
    ("zh-en", "开发团队重构了数据库的索引结构以提升查询速度。",
     "The development team restructured the database index to speed up queries.", "technology"),
    ("zh-en", "学生需要在期末考试前完成课程论文。",
     "Students need to finish their course papers before the final exam.", "education"),
    ("zh-en", "演讲者分享了他在乡村生活的经历。",
     "The speaker shared his experiences of living in the countryside.", "out_of_scope"),
]

_LEGAL_RETURN_DOMAINS = set(DOMAIN_LABELS)
_REQUIRED_EVIDENCE_KEYS = (
    "method", "training_label_quality", "formal_business_accuracy", "business_gate",
    "prototype_seed_fingerprint", "temperature",
)


def _validate_payload(payload: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload 不是 dict"]
    for key in ("domain", "subdomain", "domain_confidence", "candidates",
                "evidence", "model_version"):
        if key not in payload:
            errors.append("缺少必需键：" + key)
    if payload.get("domain") not in _LEGAL_RETURN_DOMAINS:
        errors.append("domain 不在合法集合：" + repr(payload.get("domain")))
    confidence = payload.get("domain_confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
            or not 0.0 <= float(confidence) <= 1.0:
        errors.append("domain_confidence 不是 [0,1] 数值：" + repr(confidence))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        errors.append("candidates 不是非空 list")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence or not isinstance(evidence[0], dict):
        errors.append("evidence 不是非空 dict 列表")
    else:
        for key in _REQUIRED_EVIDENCE_KEYS:
            if key not in evidence[0]:
                errors.append("evidence 缺少溯源字段：" + key)
        if evidence[0].get("method") != "zero_shot_prototype":
            errors.append("evidence method 不是 zero_shot_prototype")
        if evidence[0].get("formal_business_accuracy") != FORMAL_BUSINESS_ACCURACY:
            errors.append("evidence formal_business_accuracy 未保持 blocked 标注")
        if evidence[0].get("business_gate") != BUSINESS_GATE:
            errors.append("evidence business_gate 不是 waiting_manual")
    if not isinstance(payload.get("model_version"), str) \
            or MODEL_VERSION not in payload["model_version"]:
        errors.append("model_version 缺少 " + MODEL_VERSION)
    return errors


def _caller_parser_check(payload: dict) -> str:
    """用算子16 自己的 _parse_model_payload 校验调用方契约（不可导入时仅提示）。"""
    operator_dir = HERE.parents[1] / "PRLt_domain_detection_filter"
    sys.path.insert(0, str(operator_dir))
    try:
        from utils.domain import _parse_model_payload  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001 - 缺依赖时只提示，不影响适配器自测
        return f"[WARN] 跳过调用方解析器校验：{type(exc).__name__}: {exc}"
    finally:
        if str(operator_dir) in sys.path:
            sys.path.remove(str(operator_dir))

    class _Cfg:
        domain_aliases = {}
        allow_multi_label = False
        max_candidates = 5

    parsed = _parse_model_payload(payload, _Cfg(), None)
    return (f"[OK] 调用方 _parse_model_payload 接受 payload：domain={parsed['domain']} "
            f"confidence={parsed['domain_confidence']:.4f} "
            f"candidates={len(parsed['candidates'])}")


def _self_test() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    started = time.perf_counter()
    print("=" * 78)
    print("算子16 领域识别适配器 —— Qwen3-Embedding-0.6B 零样本原型自测（真实推理，不联网）")
    print("适配器    :", Path(__file__).resolve())
    print("权重目录  :", _resolve_encoder())
    print("原型缓存  :", PROTOTYPE_PATH)
    print("设备      :", DEVICE)
    print("=" * 78)

    load_started = time.perf_counter()
    try:
        result = classify_domain(SELF_TEST_SAMPLES[0][1], SELF_TEST_SAMPLES[0][2],
                                 SELF_TEST_SAMPLES[0][0])
    except Exception as exc:  # noqa: BLE001 - 自测需要打印失败原因
        print(f"[FAIL] 首次推理失败（含模型/原型加载）：{type(exc).__name__}: {exc}")
        return 1
    load_seconds = time.perf_counter() - load_started
    print(f"[OK] 首次调用（含编码器与原型加载）耗时 {load_seconds:.2f}s")
    print(f"[OK] 原型文件存在={PROTOTYPE_PATH.is_file()} seed_sha256={SEED_FINGERPRINT[:16]}")

    failures: list[str] = []
    rows: list[dict] = []
    matched = 0
    latencies: list[float] = []
    for index, (language_pair, source, target, expected) in enumerate(SELF_TEST_SAMPLES, 1):
        call_started = time.perf_counter()
        payload = (result if index == 1 else
                   classify_domain(source, target, language_pair))
        elapsed = time.perf_counter() - call_started
        latencies.append(elapsed)
        errors = _validate_payload(payload)
        evidence = payload["evidence"][0]
        domain_match = payload.get("domain") == expected
        matched += int(domain_match)
        rows.append({
            "index": index, "expected": expected, "language_pair": language_pair,
            "domain": payload.get("domain"),
            "domain_confidence": round(float(payload["domain_confidence"]), 6),
            "accepted": float(payload["domain_confidence"]) >= ACCEPTANCE_THRESHOLD,
            "domain_match": domain_match,
            "cosine_top": round(max(evidence["cosine_similarities"].values()), 6),
            "candidates": len(payload.get("candidates") or []),
            "elapsed_seconds": round(elapsed, 3),
            "errors": errors,
        })
        status = "OK" if not errors else "FAIL"
        print(f"\n[{status}] 样例 {index} | 期望={expected} | language_pair={language_pair}")
        print(f"        domain={payload.get('domain')} "
              f"domain_confidence={payload.get('domain_confidence'):.4f} "
              f"accepted={float(payload['domain_confidence']) >= ACCEPTANCE_THRESHOLD} "
              f"domain_match={domain_match} 耗时={elapsed:.2f}s")
        print(f"        source={source}")
        print(f"        target={target}")
        print("        evidence: method={} gate={} legacy_bge_reused={}".format(
            evidence["method"], evidence["business_gate"],
            evidence["legacy_bge_logistic_reused"]))
        if errors:
            failures.extend(f"样例{index}: {message}" for message in errors)
            for message in errors:
                print("        [FAIL] " + message)

    print("\n" + "-" * 78)
    print("样例摘要（domain/confidence/accepted/domain_match）")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    print("-" * 78)
    print(_caller_parser_check(result))

    total = time.perf_counter() - started
    print("\n" + "=" * 78)
    print(f"6 条样例中 domain 与期望一致：{matched}/{len(SELF_TEST_SAMPLES)}")
    print(f"模型加载（首次调用）耗时：{load_seconds:.2f}s")
    print(f"单条推理平均耗时：{np.mean(latencies):.3f}s")
    print(f"总耗时：{total:.2f}s")
    if failures:
        print(f"结果：FAIL（{len(failures)} 项契约失败）")
        for message in failures:
            print("  - " + message)
        print("=" * 78)
        return 1
    print("结果：PASS（返回 schema 通过；置信度是零样本原型概率，未在人工 Gold 上校准，"
          "业务门禁仍为 waiting_manual）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
