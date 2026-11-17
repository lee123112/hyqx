"""算子24（对外序号 #23）：高资源语言到低资源语言的跨语言合成。

正式版默认后端：硅基流动 OpenAI 兼容 chat 接口 + Qwen/Qwen3.5-9B。
`model_path` 非空时仍可切换到本地 NLLB-200 旧分支，作为兼容回退。
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.augmentation_runtime import chat_json, load_operator_config, local_translate, run_records
from common.augmentation_fastpath import local_translate_batch

OPERATOR = "PRLt_cross_lingual_synthesis"
OPERATOR_ID = "24"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = Path(__file__).resolve().parent
MODEL_SERVICE_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT = {"operator_version": "v1.0.0", "source_high_resource_lang": "en", "target_low_resource_lang": None,
           "anchor_lang": "zh", "model_name": "Qwen/Qwen3.5-9B", "model_version": "siliconflow/Qwen3.5-9B",
           "human_review_required": True, "model_timeout_seconds": 180,
           "model_service_url": MODEL_SERVICE_URL, "model_path": None,
           "output_name": "24_cross_lingual_synthesized.jsonl", "reject_name": "24_rejects.jsonl"}

# 常见低资源语言代码 → 语言英文名，帮助 API 模型准确理解目标语种；未收录时回退原代码。
LOW_RESOURCE_LANG_NAMES = {
    "lo": "Lao", "sw": "Swahili", "my": "Burmese", "km": "Khmer", "am": "Amharic",
    "yo": "Yoruba", "si": "Sinhala", "ne": "Nepali", "mn": "Mongolian", "kk": "Kazakh",
    "uz": "Uzbek", "tl": "Filipino", "ha": "Hausa", "ig": "Igbo", "zu": "Zulu",
    "xh": "Xhosa", "so": "Somali", "ps": "Pashto", "ur": "Urdu", "bn": "Bengali",
    "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "kn": "Kannada", "gu": "Gujarati",
    "mr": "Marathi", "pa": "Punjabi", "sd": "Sindhi", "bo": "Tibetan", "ug": "Uyghur",
    "kk-cn": "Kazakh", "zh-tw": "Traditional Chinese", "yue": "Cantonese",
}

CROSS_LINGUAL_PROMPT = (
    "You are a professional translation engine. Translate the HIGH-RESOURCE text into the "
    "requested LOW-RESOURCE language.\n"
    "Rules:\n"
    "- Return ONLY one JSON object. No markdown fences, no explanations, no extra text.\n"
    "- Required field: generated_low_resource_text (string, the translation written in the "
    "low-resource language).\n"
    "- Optional field: evidence (array of short strings describing translation choices).\n"
    '- Example: {{"generated_low_resource_text": "...", "evidence": []}}\n'
    "source_language={source_lang}\n"
    "target_language={target_lang}\n"
    "target_language_name={target_lang_name}\n"
    "anchor_language={anchor_lang}\n"
    "High-resource source: {source}\n"
    "Chinese anchor (reference only, keep unchanged, do not translate it): {target}"
)


def _config(overrides=None):
    cfg = load_operator_config(BASE_DIR, DEFAULT, overrides)
    if not isinstance(cfg.get("target_low_resource_lang"), str) or not cfg["target_low_resource_lang"].strip():
        raise ValueError("必须配置 target_low_resource_lang")
    if cfg.get("human_review_required") is not True: raise ValueError("human_review_required 必须为 true")
    if not cfg.get("model_path") and not cfg.get("model_service_url"):
        raise ValueError("未配置模型后端：model_service_url 与 model_path 均为空")
    return cfg


def _use_local(config) -> bool:
    """本地 NLLB 兼容回退：仅当显式配置 model_path 时启用；默认（model_path 为空）走 API。"""
    return bool(config.get("model_path"))


def _target_lang_name(code: str) -> str:
    key = str(code or "").strip().lower()
    return LOW_RESOURCE_LANG_NAMES.get(key, str(code))


def build_prompt(source: str, target: str, config) -> str:
    return CROSS_LINGUAL_PROMPT.format(
        source_lang=config["source_high_resource_lang"],
        target_lang=config["target_low_resource_lang"],
        target_lang_name=_target_lang_name(config["target_low_resource_lang"]),
        anchor_lang=config["anchor_lang"], source=source, target=target)


def run(input_file, unique_id, output_root=None, overrides=None, **_):
    try:
        cfg = _config(overrides)

        def transform(parent, config):
            source, target = parent.get("source"), parent.get("target")
            if not isinstance(source, str) or not isinstance(target, str): raise ValueError("缺少 source 或 target")
            if _use_local(config):
                generated, model_evidence = local_translate(
                    config, source, config["source_high_resource_lang"], config["target_low_resource_lang"])
                evidence = [model_evidence]
                backend = "local_transformers"
            else:
                prompt = build_prompt(source, target, config)
                result = chat_json(config, prompt)
                generated = result.get("generated_low_resource_text")
                evidence = result.get("evidence", [])
                backend = "siliconflow_api"
            if not isinstance(generated, str) or not generated.strip(): raise ValueError("模型未返回低资源语言文本")
            return generated.strip(), target, {"source_high_resource_text": source,
                                              "target_low_resource_lang": config["target_low_resource_lang"],
                                              "human_review_status": "pending_manual",
                                              "evidence": evidence,
                                              "backend": backend,
                                              "model_service_url": config.get("model_service_url") if not _use_local(config) else None,
                                              "source_lang": config["target_low_resource_lang"],
                                              "target_lang": config["anchor_lang"]}

        def batch_transform(parents, config):
            """批处理快路径。

            NLLB 专用批量函数只在本地权重模式可用；API/默认模式逐条调用 transform，
            避免把 `local_translate_batch` 误用于远程服务。
            """
            if not _use_local(config):
                results: list = []
                for parent in parents:
                    try:
                        results.append(transform(parent, config))
                    except Exception as exc:
                        results.append(exc)
                return results
            results: list = [None] * len(parents)
            idxs: list = []
            for i, parent in enumerate(parents):
                source, target = parent.get("source"), parent.get("target")
                if not isinstance(source, str) or not isinstance(target, str):
                    results[i] = ValueError("缺少 source 或 target")
                    continue
                idxs.append(i)
            outs = local_translate_batch(config, [parents[i]["source"] for i in idxs],
                                         config["source_high_resource_lang"],
                                         config["target_low_resource_lang"])
            for i, out in zip(idxs, outs):
                if isinstance(out, Exception):
                    results[i] = out
                    continue
                generated, model_evidence = out
                generated = generated.strip()
                if not generated:
                    results[i] = ValueError("模型未返回低资源语言文本")
                    continue
                results[i] = (generated, parents[i]["target"],
                              {"source_high_resource_text": parents[i]["source"],
                               "target_low_resource_lang": config["target_low_resource_lang"],
                               "human_review_status": "pending_manual",
                               "evidence": [model_evidence],
                               "backend": "local_transformers",
                               "model_service_url": None,
                               "source_lang": config["target_low_resource_lang"],
                               "target_lang": config["anchor_lang"]})
            return results

        return run_records(input_file, unique_id, output_root or Path(__file__).parent / "output",
                           OPERATOR, "cross_lingual_synthesis", cfg, transform,
                           batch_transform=batch_transform, operator_id=OPERATOR_ID)
    except Exception as exc:
        return {"status": "failed", "outputFile": None, "rejectFile": None, "stageZip": None,
                "storageUri": None, "statistics": {},
                "message": f"配置或输入错误：{type(exc).__name__}: {exc}"}
