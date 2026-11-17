"""Shared runtime for optional parallel-corpus augmentation operators 21-24."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib import request as urlrequest

import yaml


_LOCAL_MODEL_CACHE: dict[tuple[str, str], tuple[Any, Any, str]] = {}
_LOCAL_MODEL_LOCK = threading.Lock()

# 模型根目录解析：优先环境变量 MODEL_ROOT；其次包内 models/（交付机挂载点）；
# 最后回退到同仓共享模型目录（开发验证机把权重集中放在 G:\HYQX\算子\model）。
_SHARED_MODEL_ROOT = Path(r"G:\HYQX\算子\model")


def model_root() -> Path:
    env = os.getenv("MODEL_ROOT")
    if env and Path(env).is_dir():
        return Path(env)
    local = Path(__file__).resolve().parents[1] / "models"
    if local.is_dir() and any(local.iterdir()):
        return local
    if _SHARED_MODEL_ROOT.is_dir():
        return _SHARED_MODEL_ROOT
    return local


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def task_dir(output_root: str | Path, unique_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(unique_id or "")):
        raise ValueError("uniqueId 格式非法")
    root = Path(output_root).resolve()
    path = (root / unique_id).resolve()
    path.relative_to(root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"输入必须为 JSONL 对象，第 {line_no} 行不是对象")
        rows.append(value)
    if not rows:
        raise ValueError("输入 JSONL 不能为空")
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_operator_config(package_dir: str | Path, default: dict[str, Any],
                         overrides: dict[str, Any] | None = None,
                         path_keys: Iterable[str] = ()) -> dict[str, Any]:
    """增强算子统一配置加载：代码内 DEFAULT ← `configs/config.yaml` ← 调用方 overrides。

    2026-11-01 修复：此前增强算子（对外 20–23）的 `_config` 只有「DEFAULT + overrides」，
    `configs/config.yaml` 形同虚设——改 yaml 不生效，只能靠 overrides 注入。现与 01–19
    号算子的口径一致：yaml 是正式默认值来源，override 只用于单次调用覆盖。

    - 与 01–19 号算子一致，忽略 yaml 顶层的 `operator:` 分组键；
    - yaml 缺失或为空时不报错，退回代码内 DEFAULT（保持无 yaml 也能启动）；
    - `path_keys` 里的相对路径按算子包根目录解析，不受进程 CWD 影响。
    """
    cfg = dict(default)
    package = Path(package_dir).resolve()
    config_path = package / "configs" / "config.yaml"
    if config_path.is_file():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("配置文件必须是对象")
        cfg.update({key: value for key, value in raw.items() if key != "operator"})
    cfg.update(overrides or {})
    for key in path_keys:
        value = cfg.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value)
            if not path.is_absolute():
                cfg[key] = str((package / path).resolve())
    return cfg


def _load_env() -> None:
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        break


def _local_device(torch_module: Any) -> str:
    return "cuda" if torch_module.cuda.is_available() else "cpu"


# Qwen3.5 系列是多模态后训练架构，文本推理也必须用 AutoModelForImageTextToText；
# AutoModelForCausalLM 无法加载（config model_type=qwen3_5 无对应 causal 映射）。
_MULTIMODAL_TEXT_MODEL_TYPES = {"qwen3_5"}
_MULTIMODAL_TEXT_ARCHITECTURES = {"Qwen3_5ForConditionalGeneration"}


def _model_config_facts(path: Path) -> tuple[str, tuple[str, ...]]:
    """读取本地 config.json 的 model_type 与 architectures；缺失/损坏返回空值。"""
    config_file = path / "config.json"
    if not config_file.is_file():
        return "", ()
    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ()
    if not isinstance(raw, dict):
        return "", ()
    model_type = str(raw.get("model_type") or "").strip().lower()
    architectures = raw.get("architectures") or []
    if not isinstance(architectures, list):
        architectures = [architectures]
    return model_type, tuple(str(item) for item in architectures)


def is_multimodal_text_model(model_path: str | Path) -> bool:
    """判断本地模型是否需要按图文/多模态条件生成模型（Qwen3.5）加载。"""
    model_type, architectures = _model_config_facts(Path(model_path).resolve())
    return (model_type in _MULTIMODAL_TEXT_MODEL_TYPES
            or any(item in _MULTIMODAL_TEXT_ARCHITECTURES for item in architectures))


def _from_pretrained_text(model_cls: Any, path: Path, dtype: Any,
                          low_cpu_mem_usage: bool = False) -> Any:
    """统一 dtype 参数名：transformers>=5 用 dtype，旧版回退 torch_dtype。"""
    kwargs: dict[str, Any] = {"local_files_only": True, "trust_remote_code": True}
    if low_cpu_mem_usage:
        kwargs["low_cpu_mem_usage"] = True
    try:
        return model_cls.from_pretrained(str(path), dtype=dtype, **kwargs)
    except TypeError:
        return model_cls.from_pretrained(str(path), torch_dtype=dtype, **kwargs)


def _load_local_causal(model_path: str) -> tuple[Any, Any, str]:
    """Load and cache a local text-generation model for JSON generation.

    Qwen3.5（model_type=qwen3_5 / Qwen3_5ForConditionalGeneration）是多模态后训练
    架构，需走 AutoModelForImageTextToText；其余 Qwen2.5 等因果 LM 保持
    AutoModelForCausalLM 原路径。CPU 上 Qwen3.5 用 fp16（本机实测可加载生成），
    其余架构维持 fp32。
    """
    key = ("causal", str(Path(model_path).resolve()))
    with _LOCAL_MODEL_LOCK:
        if key in _LOCAL_MODEL_CACHE:
            return _LOCAL_MODEL_CACHE[key]
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except Exception as exc:  # pragma: no cover - dependency failure is environment-specific
            raise RuntimeError("本地 Qwen 推理依赖不可用") from exc
        path = Path(model_path).resolve()
        if not path.exists():
            raise RuntimeError(f"本地模型路径不存在: {path}")
        device = _local_device(torch)
        multimodal = is_multimodal_text_model(path)
        if device == "cuda" or multimodal:
            dtype = torch.float16
        else:
            dtype = torch.float32
        tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=True)
        if multimodal:
            try:
                from transformers import AutoModelForImageTextToText
            except Exception as exc:  # pragma: no cover - transformers 版本过旧
                raise RuntimeError(
                    "本地 Qwen3.5 需要 transformers 的 AutoModelForImageTextToText") from exc
            model = _from_pretrained_text(
                AutoModelForImageTextToText, path, dtype, low_cpu_mem_usage=True)
        else:
            model = _from_pretrained_text(AutoModelForCausalLM, path, dtype)
        model = model.to(device)
        model.eval()
        loaded = (tokenizer, model, device)
        _LOCAL_MODEL_CACHE[key] = loaded
        return loaded


def _load_local_translation(model_path: str) -> tuple[Any, Any, str]:
    """Load and cache a local NLLB sequence-to-sequence model."""
    key = ("seq2seq", str(Path(model_path).resolve()))
    with _LOCAL_MODEL_LOCK:
        if key in _LOCAL_MODEL_CACHE:
            return _LOCAL_MODEL_CACHE[key]
        try:
            import torch
            from transformers import AutoConfig, AutoModelForSeq2SeqLM, AutoTokenizer
        except Exception as exc:  # pragma: no cover - dependency failure is environment-specific
            raise RuntimeError("本地 NLLB 推理依赖不可用") from exc
        path = Path(model_path).resolve()
        if not path.exists():
            raise RuntimeError(f"本地模型路径不存在: {path}")
        device = _local_device(torch)
        dtype = torch.float16 if device == "cuda" else torch.float32
        tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, src_lang="eng_Latn")
        try:
            model = AutoModelForSeq2SeqLM.from_pretrained(
                str(path), local_files_only=True, trust_remote_code=True,
                torch_dtype=dtype,
            )
        except ValueError as exc:
            # transformers >=4.57 blocks legacy .bin loading with torch <2.6.
            # This path is only for a locally supplied, trusted checkpoint and
            # avoids any network access while preserving the model architecture.
            if "torch.load" not in str(exc) or not (path / "pytorch_model.bin").is_file():
                raise
            config = AutoConfig.from_pretrained(str(path), local_files_only=True, trust_remote_code=True)
            model = AutoModelForSeq2SeqLM.from_config(config, trust_remote_code=True)
            state = torch.load(str(path / "pytorch_model.bin"), map_location="cpu", weights_only=False)
            if isinstance(state, dict) and "state_dict" in state and isinstance(state["state_dict"], dict):
                state = state["state_dict"]
            if not isinstance(state, dict):
                raise RuntimeError("NLLB 本地权重格式不是 state_dict")
            model.load_state_dict(state, strict=False)
        model = model.to(device)
        model.eval()
        loaded = (tokenizer, model, device)
        _LOCAL_MODEL_CACHE[key] = loaded
        return loaded


_NLLB_LANGS = {
    "en": "eng_Latn", "zh": "zho_Hans", "zh-cn": "zho_Hans", "zh-hans": "zho_Hans",
    "zh-tw": "zho_Hant", "zh-hant": "zho_Hant", "fr": "fra_Latn", "de": "deu_Latn",
    "es": "spa_Latn", "it": "ita_Latn", "pt": "por_Latn", "ru": "rus_Cyrl",
    "ja": "jpn_Jpan", "ko": "kor_Hang", "ar": "arb_Arab", "hi": "hin_Deva",
    "th": "tha_Thai", "vi": "vie_Latn", "lo": "lao_Laoo", "my": "mya_Mymr",
    "km": "khm_Khmr", "sw": "swh_Latn", "am": "amh_Ethi", "yo": "yor_Latn",
}


def _nllb_lang_code(tokenizer: Any, value: str) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    candidate = _NLLB_LANGS.get(normalized, normalized)
    available = getattr(tokenizer, "lang_code_to_id", {})
    if not available and hasattr(tokenizer, "get_vocab"):
        vocab = tokenizer.get_vocab()
        available = {token: token_id for token, token_id in vocab.items()
                     if "_" in token and len(token.rsplit("_", 1)[-1]) == 4}
    if candidate in available:
        return candidate
    prefix = normalized.split("-", 1)[0]
    for code in available:
        if code.split("_", 1)[0] == prefix:
            return code
    raise ValueError(f"NLLB 不支持语言代码: {value}")


def local_translate(config: dict[str, Any], text: str, source_lang: str, target_lang: str) -> tuple[str, dict[str, Any]]:
    """Translate text with the configured local NLLB model."""
    model_path = config.get("model_path")
    if not model_path:
        raise RuntimeError("未配置本地翻译模型路径")
    tokenizer, model, device = _load_local_translation(str(model_path))
    import torch
    src_code = _nllb_lang_code(tokenizer, source_lang)
    tgt_code = _nllb_lang_code(tokenizer, target_lang)
    tokenizer.src_lang = src_code
    encoded = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    encoded = {name: value.to(device) for name, value in encoded.items()}
    lang_ids = getattr(tokenizer, "lang_code_to_id", None)
    forced_bos = lang_ids[tgt_code] if lang_ids else tokenizer.convert_tokens_to_ids(tgt_code)
    with torch.inference_mode():
        generated = model.generate(**encoded, forced_bos_token_id=forced_bos, max_new_tokens=256,
                                   num_beams=4, do_sample=False)
    result = tokenizer.batch_decode(generated, skip_special_tokens=True)[0].strip()
    if not result:
        raise RuntimeError("本地 NLLB 未生成翻译文本")
    return result, {"backend": "local_transformers", "device": device,
                    "model_path": str(Path(model_path).resolve()), "source_lang": src_code,
                    "target_lang": tgt_code}


def _local_chat_json(config: dict[str, Any], prompt: str) -> dict[str, Any]:
    tokenizer, model, device = _load_local_causal(str(config["model_path"]))
    import torch
    messages = [{"role": "system", "content": "Return only a valid JSON object."},
                {"role": "user", "content": prompt}]
    if hasattr(tokenizer, "apply_chat_template"):
        inputs = tokenizer.apply_chat_template(messages, add_generation_prompt=True,
                                                return_tensors="pt", return_dict=True)
    else:  # pragma: no cover - Qwen tokenizers provide the chat template
        inputs = tokenizer(prompt, return_tensors="pt", return_dict=True)
    inputs = {name: value.to(device) for name, value in inputs.items()}
    input_len = inputs["input_ids"].shape[-1]
    # CPU 上的 Qwen3.5 解码很慢（约 1.5 s/token），单条路径同样遵守
    # gen_max_new_tokens 配置；未配置时沿用历史默认 384。
    max_new_tokens = max(1, int(config.get("gen_max_new_tokens") or 384))
    with torch.inference_mode():
        generated = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False,
                                   pad_token_id=getattr(tokenizer, "pad_token_id", None)
                                   or getattr(tokenizer, "eos_token_id", None))
    text = tokenizer.decode(generated[0][input_len:], skip_special_tokens=True).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("本地 Qwen 响应不是有效 JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("本地 Qwen 响应 JSON 不是对象")
    result.setdefault("evidence", [])
    result["_local_model_evidence"] = {"backend": "local_transformers", "device": device,
                                        "model_path": str(Path(config["model_path"]).resolve())}
    return result


def chat_json(config: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Call an explicitly configured model service and require JSON output.

    The development-validation package is local-model-only.  Missing 21/23/24
    weights therefore produce a structured failure instead of silently falling
    back to a SiliconFlow/API endpoint from the environment.
    """
    _load_env()
    url = config.get("model_service_url")
    if not url:
        if config.get("model_path"):
            return _local_chat_json(config, prompt)
        raise RuntimeError("本地模型未配置；当前开发验证版本不允许回退到远程模型服务")
    model = config.get("model_name")
    if not model:
        raise ValueError("model_name 未配置")
    # SiliconFlow Qwen3.5-9B 实测注意事项（2026-10-31）：
    # 1) 默认开启 thinking，短句也会产生约 1900 推理 token（57s/条），长 prompt 会超过 180s 超时；
    # 2) 传 response_format={"type":"json_object"} 会挂起直至超时；
    # 因此显式关闭 thinking、限制 max_tokens，并由提示词本身约束只输出 JSON。
    max_tokens = int(config.get("api_max_tokens") or config.get("gen_max_new_tokens") or 512)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": "Return only a valid JSON object."},
                     {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "enable_thinking": False,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = os.getenv("SILICONFLOW_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=float(config.get("model_timeout_seconds", 60))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("模型服务调用失败：" + type(exc).__name__) from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型响应缺少 choices[0].message.content") from exc
    if isinstance(content, dict):
        result = content
    else:
        text = str(content).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("模型响应不是有效 JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("模型响应 JSON 不是对象")
    return result


def base_augmented(parent: dict[str, Any], operator: str, kind: str, index: int,
                   source: str, target: str, config: dict[str, Any], **extra: Any) -> dict[str, Any]:
    parent_id = parent.get("id")
    if not isinstance(parent_id, str) or not parent_id.strip():
        raise ValueError("输入记录缺少有效 id")
    value = dict(parent)
    output_source_lang = extra.pop("source_lang", None)
    output_target_lang = extra.pop("target_lang", None)
    operator_number = {"PRLt_back_translation": "21", "PRLt_synonym_substitution": "22",
                       "PRLt_syntactic_transformation": "23", "PRLt_cross_lingual_synthesis": "24"}.get(operator, operator)
    value.update({
        "id": f"{parent_id}__aug{operator_number}__{index:04d}",
        "parent_pair_id": parent_id,
        "source": source,
        "target": target,
        "augmentation_operator": operator,
        "augmentation_type": kind,
        "augmentation_status": "pending_qc",
        "human_review_status": extra.pop("human_review_status", "not_required"),
        "delivery_eligible": False,
    })
    if output_source_lang:
        value["source_lang"] = output_source_lang
    if output_target_lang:
        value["target_lang"] = output_target_lang
    metadata = dict(parent.get("metadata") or {})
    if output_source_lang and output_target_lang:
        metadata["language_pair"] = f"{output_source_lang}-{output_target_lang}"
    metadata.update({
        "parent_pair_id": parent_id,
        "augmentation_operator": operator,
        "augmentation_type": kind,
        "augmentation_status": "pending_qc",
        "human_review_status": value["human_review_status"],
        "delivery_eligible": False,
        "model_name": config.get("model_name"),
        "model_version": config.get("model_version"),
        "config_hash": config_hash(config),
        "generation_evidence": extra,
    })
    value["metadata"] = metadata
    return value


def run_records(input_file: str, unique_id: str, output_root: str | Path,
                operator: str, kind: str, config: dict[str, Any],
                transform: Callable[[dict[str, Any], dict[str, Any]], tuple[str, str, dict[str, Any]]],
                batch_transform: Callable[[list[dict[str, Any]], dict[str, Any]], list[Any]] | None = None,
                operator_id: str = "") -> dict[str, Any]:
    started = now_iso()
    tdir = task_dir(output_root, unique_id)
    output_file = tdir / config["output_name"]
    reject_file = tdir / config["reject_name"]
    # [无血缘改造 2026-10-19] 不再生成 lineage 文件。
    # lineage_file = tdir / "lineage.json"
    outputs: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    rows = read_jsonl(input_file)
    # 批处理快路径：gen_batch_size>1 且提供 batch_transform 时一次性算完全部记录；
    # 批处理整体失败或返回长度不符时回退逐条路径，保证链路韧性
    precomputed: list[Any] | None = None
    generation_path = "single"
    if (batch_transform is not None and int(config.get("gen_batch_size") or 1) > 1
            and config.get("model_path") and not config.get("model_service_url")):
        try:
            candidate = batch_transform(rows, config)
            if isinstance(candidate, list) and len(candidate) == len(rows):
                precomputed = candidate
                generation_path = "batched"
        except Exception:
            precomputed = None
    for line_no, parent in enumerate(rows, 1):
        try:
            if precomputed is not None:
                pre = precomputed[line_no - 1]
                if isinstance(pre, Exception):
                    raise pre
                source, target, evidence = pre
            else:
                source, target, evidence = transform(parent, config)
            if not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip():
                raise ValueError("增强结果文本为空")
            if source.strip() == str(parent.get("source", "")).strip() and target.strip() == str(parent.get("target", "")).strip():
                raise ValueError("增强结果与原句对完全相同")
            outputs.append(base_augmented(parent, operator, kind, len(outputs) + 1, source, target, config, **evidence))
        except Exception as exc:
            rejects.append({"line": line_no, "id": parent.get("id"), "reasons": [str(exc)],
                            "augmentation_operator": operator, "augmentation_type": kind,
                            "delivery_eligible": False})
    write_jsonl(output_file, outputs)
    write_jsonl(reject_file, rejects)
    status = "success" if outputs and not rejects else "partial" if outputs else "failed"
    if not outputs and not rejects:
        status, message = "failed", "输入 JSONL 不能为空"
    else:
        message = "" if outputs else "没有生成有效增强样本"
    stats = {"input_records": len(rows), "output_records": len(outputs),
             "rejected_records": len(rejects), "augmentation_operator": operator,
             "augmentation_type": kind, "delivery_eligible_records": 0,
             "pending_qc_records": len(outputs), "generation_path": generation_path}
    # [无血缘改造 2026-10-19] 血缘输出已停用；改为打包阶段产物并登记对象存储地址。
    # lineage = [{"operator": operator, "operator_version": config.get("operator_version", "v1.0.0"),
    #             "uniqueId": unique_id, "inputFile": str(Path(input_file).resolve()),
    #             "outputFile": str(output_file.resolve()), "rejectFile": str(reject_file.resolve()),
    #             "status": status, "startedAt": started, "finishedAt": now_iso(),
    #             "config": config, "statistics": stats}]
    # lineage_file.write_text(json.dumps(lineage, ensure_ascii=False, indent=2), encoding="utf-8")
    from common.artifacts import finalize_stage_artifacts
    stage_zip, storage_uri = finalize_stage_artifacts(
        tdir, operator_id or operator, unique_id, output_file, reject_file, tdir / "run.log")
    return {"status": status, "outputFile": str(output_file), "rejectFile": str(reject_file),
            "stageZip": stage_zip, "storageUri": storage_uri,
            "statistics": stats, "message": message}
