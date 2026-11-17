"""算子23：模型辅助句式变换，要求结构化 JSON 输出。"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.augmentation_runtime import chat_json, load_operator_config, model_root, run_records
from common.augmentation_fastpath import chat_json_batch

OPERATOR = "PRLt_syntactic_transformation"
OPERATOR_ID = "23"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = Path(__file__).resolve().parent
DEFAULT = {"operator_version": "v1.0.3", "model_name": "Qwen3.5-2B",
           "model_version": "Qwen3.5-2B", "transform_types": ["active_passive", "split", "merge"],
           "max_candidates_per_input": 1, "model_timeout_seconds": 900, "model_service_url": None,
           "model_path": str(model_root() / "Qwen3.5-2B"),
           # 批处理快路径（common.augmentation_fastpath.chat_json_batch）：默认开启。
           # 此前默认 gen_batch_size=1 使 run_records 走 augmentation_runtime._local_chat_json
           # 单条贪心路径（无 JSON 修复、无重试），模型复述原文/非 JSON 时零产出。
           # 本地 Qwen3.5-2B 在 CPU 上约 1.5 s/token（无加速内核），故单条
           # gen_max_new_tokens 收窄到 128，防止单条最坏耗时失控。
           "gen_batch_size": 4, "gen_num_beams": 1, "gen_max_new_tokens": 128,
           "gen_json_repair": True,
           # 批路径失败重试：先采样 temperature=0.7，仍失败再升温到 0.9（束搜索是确定性
           # 的，原样重试必然同样失败；采样给复述/坏 JSON 的失败记录第二次、第三次机会）。
           "gen_retry_sample": True, "gen_retry_temperature": 0.7,
           "gen_retry_escalate": True, "gen_retry_temperature_escalated": 0.9,
           "output_name": "23_syntactic_transformed.jsonl", "reject_name": "23_rejects.jsonl"}


def _config(overrides=None):
    cfg = load_operator_config(BASE_DIR, DEFAULT, overrides, path_keys=("model_path",))
    allowed = {"active_passive", "split", "merge"}
    if not cfg.get("transform_types") or not set(cfg["transform_types"]) <= allowed: raise ValueError("transform_types 配置非法")
    if int(cfg["max_candidates_per_input"]) != 1: raise ValueError("一期每条最多生成 1 个候选")
    if int(cfg.get("gen_batch_size") or 1) < 1: raise ValueError("gen_batch_size 必须 >= 1")
    return cfg


def _prompt(source, target, config):
    """句式变换提示词：附合法 JSON 输出示例，明确不得复制输入、只输出 JSON。"""
    kinds = ", ".join(config["transform_types"])
    return (
        "You rewrite a parallel sentence pair with ONE concrete syntactic transformation. "
        "The meaning must stay the same; the language of each side must stay unchanged.\n"
        f"Choose transform_type from: {kinds}.\n"
        "- active_passive: turn the active voice into the passive voice (or passive into active) "
        "on both source and target;\n"
        "- split: split a sentence into two shorter sentences on both sides;\n"
        "- merge: merge the clauses of the pair into one sentence on both sides.\n"
        "Rules:\n"
        "1. Transform BOTH sides. The new source MUST differ from the input source, and the new "
        "target MUST differ from the input target. Copying either input sentence unchanged is a failure.\n"
        "2. Output exactly one JSON object and nothing else (no markdown fences, no explanation).\n"
        "3. Required fields: source (string), target (string), transform_type (string), "
        "evidence (array of short strings).\n"
        "Example: input pair "
        '{"source": "委员会批准了这项提案。", "target": "The committee approved the proposal."} '
        "produces the legal response "
        '{"source": "这项提案获得了委员会的批准。", "target": "The proposal was approved by the committee.", '
        '"transform_type": "active_passive", '
        '"evidence": ["active voice converted to passive voice on both sides"]}\n'
        "(The example only shows the expected format and transformation style; do not reuse its content.)\n"
        f"transform_type must be one of: {kinds}.\n"
        f"Input source: {source}\nInput target: {target}")


def _parent_texts(parent):
    """读取父句对的双语文本；缺失或空白必须在调用模型前拦截。

    只校验 isinstance 会让空 target 进入模型，模型会替缺失的一侧「补写」内容，
    产出看似合法、实际是编造的句对——这类缺陷不会报错，只会污染语料。
    """
    source, target = parent.get("source"), parent.get("target")
    if not isinstance(source, str) or not isinstance(target, str):
        raise ValueError("缺少 source 或 target")
    if not source.strip() or not target.strip():
        raise ValueError("输入 source 或 target 为空")
    return source.strip(), target.strip()


def _require_both_changed(parent, source, target):
    """句式变换要求双侧都改；只改一侧不算完成变换（提示词第 1 条即此约束）。

    只拒绝「整条相同」会放过单侧原样返回，而句式变换的定义是双侧句法都变，
    单侧未变说明模型只做了局部改写或直接复制。
    """
    unchanged = [name for name, before, after in (
        ("source", str(parent.get("source", "")).strip(), source),
        ("target", str(parent.get("target", "")).strip(), target)) if before == after]
    if len(unchanged) == 2:
        raise ValueError("增强结果与原句对完全相同")
    if unchanged:
        raise ValueError(f"增强结果 {unchanged[0]} 未发生变化")
    return source, target


def _unpack(result, config):
    """校验模型结构化输出；失败抛带具体原因的 ValueError（进拒绝清单）。"""
    out_source, out_target, kind = result.get("source"), result.get("target"), result.get("transform_type")
    if not all(isinstance(v, str) and v.strip() for v in (out_source, out_target)):
        raise ValueError("模型未返回有效 source/target")
    if kind not in config["transform_types"]:
        raise ValueError("模型返回了未配置的 transform_type")
    return out_source.strip(), out_target.strip(), {"transform_type": kind,
                                                    "evidence": result.get("evidence", []),
                                                    "model_evidence": result.get("_local_model_evidence")}


def run(input_file, unique_id, output_root=None, overrides=None, **_):
    try:
        cfg = _config(overrides)

        def transform(parent, config):
            source, target = _parent_texts(parent)
            out_source, out_target, evidence = _unpack(
                chat_json(config, _prompt(source, target, config)), config)
            _require_both_changed(parent, out_source, out_target)
            return out_source, out_target, evidence

        def batch_transform(parents, config):
            """批处理快路径：左填充批量生成，逐条解包校验，语义与逐条路径一致。

            失败记录（无效 JSON / 解包失败 / 与原句完全相同）逐级重试：
            1) 采样重试（gen_retry_sample，temperature=gen_retry_temperature，默认 0.7）；
            2) 仍失败再升温重试（gen_retry_escalate，temperature=gen_retry_temperature_escalated，
               默认 0.9）。束搜索是确定性的，原样重试必然同样失败，采样才是有效第二次机会。
            重试成功候选在 model_evidence 标记 sampling_retry / retry_temperature /
            retry_escalated；每轮保留最新具体失败原因（供拒绝统计）。
            """
            results: list = [None] * len(parents)
            prompt_by_idx: dict[int, str] = {}
            for i, parent in enumerate(parents):
                try:
                    source, target = _parent_texts(parent)
                except ValueError as exc:
                    results[i] = exc
                    continue
                prompt_by_idx[i] = _prompt(source, target, config)

            def _attempt(i, out):
                """解包并校验；返回 (source, target, meta) 或带具体原因的 Exception。"""
                if isinstance(out, Exception):
                    return out
                try:
                    unpacked = _unpack(out, config)
                    _require_both_changed(parents[i], unpacked[0], unpacked[1])
                except Exception as exc:
                    return exc
                return unpacked

            def _mark(unpacked, meta):
                evidence = dict(unpacked[2])
                model_ev = dict(evidence.get("model_evidence") or {})
                model_ev.update(meta)
                evidence["model_evidence"] = model_ev
                return unpacked[0], unpacked[1], evidence

            def _run_pass(idxs, cfg, mark, hint=""):
                pending = []
                try:
                    outs = chat_json_batch(cfg, [prompt_by_idx[i] + hint for i in idxs])
                except Exception as exc:  # 整批不可用：记录异常，留待下一轮重试
                    for i in idxs:
                        results[i] = exc
                    return list(idxs)
                for i, out in zip(idxs, outs):
                    attempted = _attempt(i, out)
                    if isinstance(attempted, Exception):
                        results[i] = attempted  # 保留最新具体失败原因（供拒绝统计）
                        pending.append(i)
                        continue
                    results[i] = _mark(attempted, mark) if mark else attempted
                return pending

            pending = _run_pass(sorted(prompt_by_idx), config, mark=None)
            retry_on = str(config.get("gen_retry_sample", "true")).strip().lower() \
                not in {"0", "false", "no"}
            escalate_on = str(config.get("gen_retry_escalate", "true")).strip().lower() \
                not in {"0", "false", "no"}
            # 重试提示语：直击本次根因（模型原样复述输入），不改变拒绝口径
            retry_hint = ("\nYour previous answer was rejected (it copied the input or was not valid JSON). "
                          "Return a real transformation: the new source and target MUST differ from the input "
                          "sentences above and must still be a legal JSON object.")
            if pending and retry_on:
                temperature = float(config.get("gen_retry_temperature") or 0.7)
                pending = _run_pass(
                    pending,
                    dict(config, gen_do_sample=True, gen_temperature=temperature, gen_num_beams=1),
                    mark={"sampling_retry": True, "retry_temperature": temperature},
                    hint=retry_hint)
            if pending and escalate_on:
                temperature = float(config.get("gen_retry_temperature_escalated") or 0.9)
                _run_pass(
                    pending,
                    dict(config, gen_do_sample=True, gen_temperature=temperature, gen_num_beams=1),
                    mark={"sampling_retry": True, "retry_temperature": temperature,
                          "retry_escalated": True},
                    hint=retry_hint)
            return results

        return run_records(input_file, unique_id, output_root or Path(__file__).parent / "output",
                           OPERATOR, "syntactic_transformation", cfg, transform,
                           batch_transform=batch_transform, operator_id=OPERATOR_ID)
    except Exception as exc:
        return {"status": "failed", "outputFile": None, "rejectFile": None, "stageZip": None,
                "storageUri": None, "statistics": {},
                "message": f"配置或输入错误：{type(exc).__name__}: {exc}"}
