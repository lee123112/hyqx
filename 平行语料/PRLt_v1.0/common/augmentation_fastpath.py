"""增强算子批量生成快路径：padding 批处理 + 可配置束宽/贪心。

仅当 config["gen_batch_size"] > 1 时由 run_records 的批处理分支调用；默认关闭，
既有逐条路径行为不变。任一环节失败由调用方按记录降级或整体回退逐条路径。

配置旋钮（overrides 透传）：
- gen_batch_size: 批大小（默认 1 = 关闭批处理）
- gen_num_beams: 束宽（默认 None = 沿用单路径惯例：翻译 4 / 对话 1；设 1 为贪心吞吐模式）
- gen_max_new_tokens: 最大新 token（默认 None = 翻译 256 / 对话 384）
- gen_do_sample / gen_temperature / gen_top_p: 采样模式（默认关闭；失败重试时由
  处理器传入开启，束搜索是确定性的，对失败记录原样重试必然同样失败）
- gen_json_repair: 对话 JSON 截断/杂质修复（默认开启；提取平衡括号块并闭合截断）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common.augmentation_runtime import (
    _load_local_causal,
    _load_local_translation,
    _nllb_lang_code,
)


def _gen_knobs(config: dict[str, Any], default_beams: int, default_max_new: int) -> tuple[int, int, int]:
    batch_size = max(1, int(config.get("gen_batch_size") or 1))
    beams = config.get("gen_num_beams")
    beams = default_beams if beams is None else max(1, int(beams))
    max_new = config.get("gen_max_new_tokens")
    max_new = default_max_new if max_new is None else max(1, int(max_new))
    return batch_size, beams, max_new


def repair_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出中提取/修复单个 JSON 对象；无法修复返回 None。

    覆盖两类常见失败：输出带前言/后缀杂质（提取第一个平衡 {} 块）、
    max_new_tokens 截断（闭合未收尾的字符串与括号）、末尾多余逗号。
    """
    start = text.find("{")
    if start < 0:
        return None
    stack: list[str] = []
    in_str = False
    esc = False
    end = -1
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return None
            stack.pop()
            if not stack:
                end = pos
                break
    chunk = text[start:end + 1] if end >= 0 else text[start:]
    if end < 0:  # 截断：闭合未收尾的字符串与括号
        if in_str:
            chunk += '"'
        chunk += "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    chunk = re.sub(r",\s*([}\]])", r"\1", chunk)  # 尾逗号非法，修复时顺带清理
    try:
        payload = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def local_translate_batch(config: dict[str, Any], texts: list[str],
                          source_lang: str, target_lang: str) -> list[Any]:
    """批量 NLLB 翻译；返回与 texts 等长的列表，元素为 (译文, evidence) 或 Exception。"""
    model_path = config.get("model_path")
    if not model_path:
        raise RuntimeError("未配置本地翻译模型路径")
    tokenizer, model, device = _load_local_translation(str(model_path))
    import torch
    batch_size, beams, max_new = _gen_knobs(config, 4, 256)
    src_code = _nllb_lang_code(tokenizer, source_lang)
    tgt_code = _nllb_lang_code(tokenizer, target_lang)
    tokenizer.src_lang = src_code
    lang_ids = getattr(tokenizer, "lang_code_to_id", None)
    forced_bos = lang_ids[tgt_code] if lang_ids else tokenizer.convert_tokens_to_ids(tgt_code)
    evidence_base = {"backend": "local_transformers", "device": device,
                     "model_path": str(Path(model_path).resolve()),
                     "source_lang": src_code, "target_lang": tgt_code,
                     "generation_mode": "batched", "batch_size": batch_size,
                     "num_beams": beams}
    results: list[Any] = [None] * len(texts)
    # 按长度排序分桶，减少 padding 浪费；恢复原始下标顺序
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    for start in range(0, len(order), batch_size):
        idxs = order[start:start + batch_size]
        try:
            encoded = tokenizer([texts[i] for i in idxs], return_tensors="pt", padding=True,
                                truncation=True, max_length=512)
            encoded = {name: value.to(device) for name, value in encoded.items()}
            with torch.inference_mode():
                generated = model.generate(**encoded, forced_bos_token_id=forced_bos,
                                           max_new_tokens=max_new, num_beams=beams,
                                           do_sample=False)
            decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
            for i, text in zip(idxs, decoded):
                text = str(text).strip()
                if text:
                    results[i] = (text, dict(evidence_base))
                else:
                    results[i] = RuntimeError("本地 NLLB 未生成翻译文本")
        except Exception as exc:  # 整批失败：批内记录各自携带异常，由上层按条拒绝
            for i in idxs:
                results[i] = exc
    return results


def chat_json_batch(config: dict[str, Any], prompts: list[str]) -> list[Any]:
    """批量本地因果 LM JSON 对话；返回与 prompts 等长的 dict 或 Exception 列表。"""
    tokenizer, model, device = _load_local_causal(str(config["model_path"]))
    import torch
    batch_size, beams, max_new = _gen_knobs(config, 1, 384)
    do_sample = bool(config.get("gen_do_sample"))
    gen_kwargs: dict[str, Any] = {"max_new_tokens": max_new,
                                  "pad_token_id": tokenizer.pad_token_id or tokenizer.eos_token_id}
    if do_sample:  # 采样重试：束搜索是确定性的，失败记录原样重试必然同样失败
        gen_kwargs.update({"do_sample": True, "num_beams": 1,
                           "temperature": float(config.get("gen_temperature") or 0.7),
                           "top_p": float(config.get("gen_top_p") or 0.9)})
    else:
        gen_kwargs.update({"do_sample": False, "num_beams": beams})
    json_repair = bool(config.get("gen_json_repair", True))
    prev_padding_side = getattr(tokenizer, "padding_side", "right")
    tokenizer.padding_side = "left"  # 因果 LM 批解码须左填充，新段为末尾定长切片
    if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
        tokenizer.pad_token = tokenizer.eos_token
    rendered: list[str] = []
    for prompt in prompts:
        messages = [{"role": "system", "content": "Return only a valid JSON object."},
                    {"role": "user", "content": prompt}]
        if hasattr(tokenizer, "apply_chat_template"):
            rendered.append(tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True))
        else:  # pragma: no cover - Qwen tokenizers provide the chat template
            rendered.append(prompt)
    results: list[Any] = [None] * len(prompts)
    order = sorted(range(len(rendered)), key=lambda i: len(rendered[i]))
    try:
        for start in range(0, len(order), batch_size):
            idxs = order[start:start + batch_size]
            try:
                inputs = tokenizer([rendered[i] for i in idxs], return_tensors="pt",
                                   padding=True, truncation=True, max_length=2048)
                inputs = {name: value.to(device) for name, value in inputs.items()}
                prompt_width = inputs["input_ids"].shape[1]
                with torch.inference_mode():
                    generated = model.generate(**inputs, **gen_kwargs)
                for row, i in enumerate(idxs):
                    text = tokenizer.decode(generated[row][prompt_width:],
                                            skip_special_tokens=True).strip()
                    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        payload = repair_json_object(text) if json_repair else None
                        if payload is None:
                            results[i] = RuntimeError("本地 Qwen 响应不是有效 JSON")
                            continue
                        repaired = True
                    else:
                        repaired = False
                    if not isinstance(payload, dict):
                        results[i] = RuntimeError("本地 Qwen 响应 JSON 不是对象")
                        continue
                    payload.setdefault("evidence", [])
                    payload["_local_model_evidence"] = {
                        "backend": "local_transformers", "device": device,
                        "model_path": str(Path(config["model_path"]).resolve()),
                        "generation_mode": "sampled" if do_sample else "batched",
                        "batch_size": batch_size}
                    if repaired:
                        payload["_local_model_evidence"]["json_repaired"] = True
                    results[i] = payload
            except Exception as exc:
                for i in idxs:
                    results[i] = exc
    finally:
        tokenizer.padding_side = prev_padding_side
    return results
