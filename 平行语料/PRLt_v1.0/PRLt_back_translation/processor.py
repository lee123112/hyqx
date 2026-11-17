"""算子21（对外序号 #20）：单轮回译增强。

正式版默认后端：硅基流动 OpenAI 兼容 chat 接口 + Qwen/Qwen3.5-9B。
`model_path` 非空时仍可切换到本地 NLLB-200 旧分支，作为兼容回退。
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Any
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.augmentation_runtime import chat_json, load_operator_config, local_translate, run_records
from common.augmentation_fastpath import local_translate_batch

OPERATOR = "PRLt_back_translation"
OPERATOR_ID = "21"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = Path(__file__).resolve().parent
MODEL_SERVICE_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT = {"operator_version": "v1.0.0", "model_name": "Qwen/Qwen3.5-9B",
           "model_version": "siliconflow/Qwen3.5-9B", "rounds": 1, "source_lang": "en", "target_lang": "zh",
           "max_candidates_per_input": 1, "model_timeout_seconds": 180,
           "model_service_url": MODEL_SERVICE_URL, "model_path": None,
           "output_name": "21_back_translated.jsonl",
           "reject_name": "21_rejects.jsonl"}

BACK_TRANSLATION_PROMPT = (
    "You are a professional translation engine. Translate the TARGET text back into the "
    "SOURCE language in exactly one round.\n"
    "Rules:\n"
    "- Return ONLY one JSON object. No markdown fences, no explanations, no extra text.\n"
    "- Required field: back_translated_source (string, the back-translation written in the "
    "source language).\n"
    "- Optional field: evidence (array of short strings describing translation choices).\n"
    '- Example: {{"back_translated_source": "...", "evidence": []}}\n'
    "source_language={source_lang}\n"
    "target_language={target_lang}\n"
    "Original source (for reference only): {source}\n"
    "Target text to translate back: {target}"
)


def _config(overrides=None):
    cfg = load_operator_config(BASE_DIR, DEFAULT, overrides)
    if cfg["rounds"] != 1: raise ValueError("rounds 必须固定为 1")
    if int(cfg["max_candidates_per_input"]) != 1: raise ValueError("一期 max_candidates_per_input 必须为 1")
    if not cfg.get("model_path") and not cfg.get("model_service_url"):
        raise ValueError("未配置模型后端：model_service_url 与 model_path 均为空")
    return cfg


def _use_local(config) -> bool:
    """本地 NLLB 兼容回退：仅当显式配置 model_path 时启用；默认（model_path 为空）走 API。"""
    return bool(config.get("model_path"))


def build_prompt(source: str, target: str, source_lang: str, target_lang: str) -> str:
    return BACK_TRANSLATION_PROMPT.format(source_lang=source_lang, target_lang=target_lang,
                                          source=source, target=target)


def run(input_file, unique_id, output_root=None, overrides=None, **_):
    try:
        cfg = _config(overrides)
    except Exception as exc:
        return {"status": "failed", "outputFile": None, "rejectFile": None, "stageZip": None,
                "storageUri": None, "statistics": {},
                "message": f"配置或输入错误：{type(exc).__name__}: {exc}"}

    def transform(parent, config):
        source, target = parent.get("source"), parent.get("target")
        if not isinstance(source, str) or not isinstance(target, str) or not source.strip() or not target.strip():
            raise ValueError("缺少 source 或 target")
        source_lang = str(parent.get("source_lang") or config["source_lang"])
        target_lang = str(parent.get("target_lang") or config["target_lang"])
        if _use_local(config):
            back, model_evidence = local_translate(config, target, target_lang, source_lang)
            evidence = [model_evidence]
            backend = "local_transformers"
        else:
            prompt = build_prompt(source, target, source_lang, target_lang)
            result = chat_json(config, prompt)
            back = result.get("back_translated_source")
            evidence = result.get("evidence", [])
            backend = "siliconflow_api"
        if not isinstance(back, str) or not back.strip(): raise ValueError("模型未返回 back_translated_source")
        return back.strip(), target, {"model_name": config["model_name"], "rounds": 1,
                                     "back_translation": back.strip(), "evidence": evidence,
                                     "backend": backend,
                                     "model_service_url": config.get("model_service_url") if not _use_local(config) else None,
                                     "source_lang": source_lang, "target_lang": target_lang}

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
        groups: dict = {}
        for i, parent in enumerate(parents):
            source, target = parent.get("source"), parent.get("target")
            if not isinstance(source, str) or not isinstance(target, str) or not source.strip() or not target.strip():
                results[i] = ValueError("缺少 source 或 target")
                continue
            source_lang = str(parent.get("source_lang") or config["source_lang"])
            target_lang = str(parent.get("target_lang") or config["target_lang"])
            groups.setdefault((target_lang, source_lang), []).append(i)
        for (target_lang, source_lang), idxs in groups.items():
            outs = local_translate_batch(config, [parents[i]["target"] for i in idxs],
                                         target_lang, source_lang)
            for i, out in zip(idxs, outs):
                if isinstance(out, Exception):
                    results[i] = out
                    continue
                back, model_evidence = out
                back = back.strip()
                if not back:
                    results[i] = ValueError("模型未返回 back_translated_source")
                    continue
                target = parents[i]["target"]
                results[i] = (back, target, {"model_name": config["model_name"], "rounds": 1,
                                             "back_translation": back, "evidence": [model_evidence],
                                             "backend": "local_transformers",
                                             "model_service_url": None,
                                             "source_lang": source_lang, "target_lang": target_lang})
        return results

    try:
        return run_records(input_file, unique_id, output_root or Path(__file__).parent / "output",
                           OPERATOR, "back_translation", cfg, transform, batch_transform=batch_transform,
                           operator_id=OPERATOR_ID)
    except Exception as exc:
        return {"status": "failed", "outputFile": None, "rejectFile": None, "stageZip": None,
                "storageUri": None, "statistics": {},
                "message": f"配置或输入错误：{type(exc).__name__}: {exc}"}
