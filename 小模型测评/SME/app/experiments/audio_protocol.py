"""Local Whisper training comparison on one fixed, speaker-held-out E.

No downloads, label fabrication, test-set tuning or audio truncation. Dataset
audits run before loading the model. Frozen ASR diagnostics remain in engines/audio.
"""
from __future__ import annotations

from .training_lock import training_locked

import gc
import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..core.envelope import ApiError
from ..engines.audio import decode_audio_bytes, normalize_text, _edit_distance
from ..engines.media import AssetUnavailable

NORMALIZATION = "nfkc-lower-letters-numbers-v1"


@dataclass(frozen=True)
class AudioProtocolSpec:
    ours_manifest: Path
    ref_manifest: Path
    eval_manifest: Path
    model_dir: Path
    seeds: tuple[int, ...] = (42, 43, 44)
    epochs: int = 1
    learning_rate: float = 1e-5
    batch_size: int = 1
    training_mode: str = "decoder"
    device: str = "auto"
    language: str = "chinese"
    purpose: str = "exploration"
    max_duration_gap: float = 0.05
    max_leakage_pairs: int = 4_000_000
    max_new_tokens: int = 128


@dataclass
class AudioRecord:
    id: str
    text: str
    speaker: str
    audio: np.ndarray
    sha256: str
    duration: float


def _error(code: str, message: str, details: Any = None) -> None:
    raise ApiError(422, code, message, details)


def _validate(spec: AudioProtocolSpec) -> None:
    ints = (spec.epochs, spec.batch_size, spec.max_leakage_pairs, spec.max_new_tokens)
    if (any(type(v) is not int or v <= 0 for v in ints)
            or spec.epochs > 100 or spec.batch_size > 32 or spec.max_new_tokens > 400
            or not spec.seeds or len(spec.seeds) > 10
            or any(type(s) is not int or not 0 <= s < 2**32 for s in spec.seeds)
            or len(set(spec.seeds)) != len(spec.seeds)
            or isinstance(spec.learning_rate, bool) or not math.isfinite(spec.learning_rate)
            or not 0 < spec.learning_rate <= 0.01
            or not math.isfinite(spec.max_duration_gap) or not 0 <= spec.max_duration_gap <= .1
            or spec.training_mode not in {"full", "decoder"}
            or spec.purpose not in {"smoke", "exploration", "quality", "formal"}
            or spec.device not in {"auto", "cpu", "cuda"}
            or spec.language not in {"chinese", "english"}):
        _error("CONFIG_INVALID", "音频协议参数无效：检查种子、训练预算、模式和归一化语言")


def _records(manifest: Path) -> list[AudioRecord]:
    try:
        content = manifest.read_text(encoding="utf-8-sig")
        if manifest.suffix.lower() == ".json":
            payload = json.loads(content)
            rows = payload.get("samples") if isinstance(payload, dict) else payload
        else:
            rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        _error("DATASET_INVALID", f"无法读取音频清单：{manifest.name}", type(exc).__name__)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        _error("DATASET_INVALID", "每份音频清单需要 1–5000 条样本")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            _error("DATASET_INVALID", "音频清单行必须为对象")
        sid = str(row.get("id") or "").strip()
        speaker = str(row.get("speaker") or row.get("speaker_id") or "").strip()
        text = row.get("text", row.get("transcript", row.get("output", "")))
        if not sid or sid in seen or not speaker:
            _error("DATASET_INVALID", "音频需要唯一 id 和真实 speaker 标签")
        seen.add(sid)
        if not isinstance(text, str) or not normalize_text(text):
            _error("TRANSCRIPT_INVALID", f"{sid} 缺少有效参考转写")
        raw = row.get("audio") or row.get("input")
        if not isinstance(raw, str) or not raw:
            _error("ASSET_MISSING", f"{sid} 缺少本地音频路径")
        path = Path(raw)
        if not path.is_absolute():
            path = manifest.parent / path
        try:
            signal, rate = decode_audio_bytes(path.read_bytes())
        except (OSError, AssetUnavailable) as exc:
            _error("ASSET_MISSING", f"{sid} 的音频缺失或不可解码", type(exc).__name__)
        duration = signal.size / rate
        if not .05 <= duration <= 30:
            _error("DATASET_INVALID", f"{sid} 时长需为 0.05–30 秒；长音频请先独立切分")
        if float(np.max(np.abs(signal))) < 1e-5:
            _error("DATASET_INVALID", f"{sid} 是静音音频")
        pcm_hash = hashlib.sha256(np.round(signal * 32767).astype("<i2").tobytes()).hexdigest()
        result.append(AudioRecord(sid, normalize_text(text), speaker, signal, pcm_hash, duration))
    return result


def _split_meta(records: list[AudioRecord]) -> dict[str, Any]:
    payload = [(r.id, r.text, r.speaker, r.sha256) for r in records]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"count": len(records), "durationSeconds": round(sum(r.duration for r in records), 4),
            "speakers": sorted({r.speaker for r in records}), "fingerprint": digest,
            "ids": [r.id for r in records]}


def _signature(record: AudioRecord) -> np.ndarray:
    # A centered, unit-normalized waveform catches gain changes, polarity,
    # headers and common PCM re-encodings. This is not a crop/semantic detector.
    positions = np.linspace(0, record.audio.size - 1, min(4096, record.audio.size)).astype(int)
    value = record.audio[positions].astype(np.float64)
    value -= value.mean()
    return value / max(float(np.linalg.norm(value)), 1e-12)


def audit_leakage(ours: list[AudioRecord], ref: list[AudioRecord],
                  evaluation: list[AudioRecord], max_pairs: int) -> dict[str, Any]:
    matches, checked = [], 0
    esigs = {r.id: _signature(r) for r in evaluation}
    for name, training in (("ours", ours), ("ref", ref)):
        speakers = {r.speaker for r in training} & {r.speaker for r in evaluation}
        if speakers:
            matches.append({"split": name, "kind": "speaker", "speakers": sorted(speakers)})
        for train in training:
            signature = _signature(train)
            for test in evaluation:
                checked += 1
                if checked > max_pairs:
                    _error("AUDIT_INCOMPLETE", "音频泄漏比对达到上限，不能判定无泄漏", {"checkedPairs": checked - 1})
                kind = None
                if train.id == test.id:
                    kind = "id"
                elif train.sha256 == test.sha256:
                    kind = "decoded_pcm"
                elif (abs(train.duration - test.duration) <= 1 / 16000
                      and signature.shape == esigs[test.id].shape
                      and abs(float(signature @ esigs[test.id])) >= .9995):
                    kind = "waveform_gain_or_reencoding"
                if kind:
                    matches.append({"split": name, "kind": kind, "trainId": train.id, "evalId": test.id})
    result = {"passed": not matches, "checkedPairs": checked, "matches": matches[:100],
              "matchCount": len(matches),
              "coverage": ["speaker", "id", "decoded_pcm", "same-length gain/polarity/reencoding"],
              "limitations": "不覆盖任意裁剪、时间平移或预训练语料污染；speaker ID 必须跨清单使用同一命名空间"}
    if matches:
        _error("LEAKAGE_DETECTED", "固定 E 与训练音频存在说话人或样本泄漏", result)
    return result


def score_transcripts(rows: list[tuple[str, str, str]]) -> dict[str, Any]:
    edits = chars = 0
    samples = []
    for sid, reference, hypothesis in rows:
        reference, hypothesis = normalize_text(reference), normalize_text(hypothesis)
        if not reference:
            _error("TRANSCRIPT_INVALID", f"{sid} 参考转写归一化后为空")
        distance = _edit_distance(reference, hypothesis)
        edits += distance
        chars += len(reference)
        samples.append({"id": sid, "reference": reference, "hypothesis": hypothesis,
                        "edits": distance, "referenceChars": len(reference),
                        "cer": round(100 * distance / len(reference), 4)})
    if not chars:
        _error("DATASET_INVALID", "固定 E 不能为空")
    return {"cer": round(100 * edits / chars, 6), "edits": edits,
            "referenceChars": chars, "sampleCount": len(samples), "samples": samples}


def _model_fingerprint(model_dir: Path) -> str:
    files = sorted(p for p in model_dir.glob("*") if p.is_file() and p.suffix in {".json", ".txt", ".bin", ".safetensors"})
    if not (model_dir / "config.json").is_file() or not any(p.suffix in {".bin", ".safetensors"} for p in files):
        _error("ASR_ASSET_MISSING", "训练式音频评估需要完整本地 Whisper 模型与 tokenizer")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _train_and_evaluate(records: list[AudioRecord], evaluation: list[AudioRecord],
                        spec: AudioProtocolSpec, seed: int) -> dict[str, Any]:
    try:
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
    except ImportError:
        _error("ENGINE_UNAVAILABLE", "音频训练需要 torch 和 transformers")
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(min(4, torch.get_num_threads()))
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    device = ("cuda" if torch.cuda.is_available() else "cpu") if spec.device == "auto" else spec.device
    if device == "cuda" and not torch.cuda.is_available():
        _error("ENGINE_UNAVAILABLE", "请求 CUDA 但当前不可用")
    model = optimizer = None
    try:
        processor = WhisperProcessor.from_pretrained(str(spec.model_dir), local_files_only=True,
                                                     language=spec.language, task="transcribe")
        model = WhisperForConditionalGeneration.from_pretrained(str(spec.model_dir), local_files_only=True)
        if spec.training_mode == "decoder":
            model.model.encoder.requires_grad_(False)
        else:
            model.gradient_checkpointing_enable()
        model.config.use_cache = False
        model.generation_config.forced_decoder_ids = None
        model.generation_config.language = spec.language
        model.generation_config.task = "transcribe"
        model.to(device)
        trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
        optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad),
                                     lr=spec.learning_rate, weight_decay=.01)

        def features(record):
            return processor.feature_extractor(record.audio, sampling_rate=16000,
                                               return_tensors="pt").input_features[0]

        training = []
        for record in records:
            tokens = processor.tokenizer(record.text).input_ids
            if tokens and tokens[0] == model.config.decoder_start_token_id:
                tokens = tokens[1:]
            if len(tokens) >= model.config.max_target_positions:
                _error("TRANSCRIPT_INVALID", f"{record.id} 转写过长，禁止截断标签")
            training.append((features(record), torch.tensor(tokens, dtype=torch.long)))
        losses, steps = [], 0
        rng = np.random.default_rng(seed)
        model.train()
        for epoch in range(spec.epochs):
            order = rng.permutation(len(training)).tolist()
            epoch_losses = []
            for offset in range(0, len(order), spec.batch_size):
                batch = [training[i] for i in order[offset:offset + spec.batch_size]]
                inputs = torch.stack([item[0] for item in batch]).to(device)
                labels = torch.nn.utils.rnn.pad_sequence([item[1] for item in batch], batch_first=True, padding_value=-100).to(device)
                optimizer.zero_grad(set_to_none=True)
                loss = model(input_features=inputs, labels=labels).loss
                if not torch.isfinite(loss):
                    _error("TRAINING_FAILED", "ASR loss 为 NaN/Inf，训练未完成")
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
                optimizer.step()
                steps += 1
                epoch_losses.append(float(loss.detach().cpu()))
            losses.append({"epoch": epoch + 1, "meanLoss": float(np.mean(epoch_losses)),
                           "stepLosses": epoch_losses})
        # E is read only here; it cannot select a checkpoint or hyperparameter.
        model.eval()
        optimizer.zero_grad(set_to_none=True)
        predictions = []
        with torch.no_grad():
            for record in evaluation:
                prediction = model.generate(features(record).unsqueeze(0).to(device),
                                            max_new_tokens=spec.max_new_tokens, do_sample=False,
                                            num_beams=1, use_cache=True)
                if prediction.shape[-1] >= spec.max_new_tokens + 1 and int(prediction[0, -1]) != model.config.eos_token_id:
                    _error("INFERENCE_INCOMPLETE", f"{record.id} ASR 输出达到 token 上限，请提高 maxNewTokens")
                text = processor.batch_decode(prediction, skip_special_tokens=True)[0]
                predictions.append((record.id, record.text, text))
        return {"scores": score_transcripts(predictions), "lossCurve": losses,
                "steps": steps, "trainableParameters": trainable, "device": device,
                "torchVersion": torch.__version__}
    finally:
        del optimizer, model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _stats(values: list[float]) -> dict[str, Any]:
    return {"mean": float(np.mean(values)),
            "std": float(np.std(values, ddof=1)) if len(values) > 1 else None,
            "values": values, "n": len(values)}


@training_locked
def run_audio_protocol(spec: AudioProtocolSpec) -> dict[str, Any]:
    _validate(spec)
    ours, ref, evaluation = (_records(Path(p)) for p in
                             (spec.ours_manifest, spec.ref_manifest, spec.eval_manifest))
    data = {name: _split_meta(records) for name, records in
            (("ours", ours), ("ref", ref), ("eval", evaluation))}
    gap = abs(data["ours"]["durationSeconds"] - data["ref"]["durationSeconds"]) / max(data["ref"]["durationSeconds"], 1e-9)
    if len(ours) != len(ref) or gap > spec.max_duration_gap:
        _error("BUDGET_MISMATCH", "D_ours/D_ref 必须等条数且总时长差不超过设定预算", {"durationGap": gap, "data": data})
    leakage = audit_leakage(ours, ref, evaluation, spec.max_leakage_pairs)
    model_hash = _model_fingerprint(Path(spec.model_dir))
    runs = []
    for seed in spec.seeds:
        left = _train_and_evaluate(ours, evaluation, spec, seed)
        right = _train_and_evaluate(ref, evaluation, spec, seed)
        if left["steps"] != right["steps"]:
            _error("BUDGET_MISMATCH", "两侧实际训练步数不一致")
        runs.append({"seed": seed, "ours": left, "ref": right,
                     "cerReductionPp": right["scores"]["cer"] - left["scores"]["cer"]})
    delta = _stats([r["cerReductionPp"] for r in runs])
    required = 5 if spec.purpose == 'formal' else 3
    eligible = spec.purpose in {'quality', 'formal'} and len(spec.seeds) >= required
    return {
        "type": "audio-quality-protocol", "version": "audio-whisper-training-v1", "valid": True,
        "qualityConclusionEligible": eligible,
        "protocol": {"task": "asr", "model": Path(spec.model_dir).name, "modelFingerprint": model_hash,
                     "trainingMode": spec.training_mode, "encoderFrozen": spec.training_mode == "decoder",
                     "seeds": list(spec.seeds), "epochs": spec.epochs, "batchSize": spec.batch_size,
                     "learningRate": spec.learning_rate, "optimizer": "AdamW", "weightDecay": .01,
                     "clipGradNorm": 1.0, "language": spec.language, "sampleRate": 16000,
                     "normalization": NORMALIZATION, "aggregation": "sum(edits)/sum(referenceChars)*100",
                     "durationGap": gap, "maxDurationGap": spec.max_duration_gap,
                     "maxNewTokens": spec.max_new_tokens, "purpose": spec.purpose,
                     "checkpointSelection": "final epoch; E never used for tuning",
                     "benchmark": "same-protocol local retraining; no paper score"},
        "data": data, "leakage": leakage,
        "S_ours": {"cer": _stats([r["ours"]["scores"]["cer"] for r in runs])},
        "S_ref": {"cer": _stats([r["ref"]["scores"]["cer"] for r in runs])},
        "delta": {"cerReductionPp": delta, "direction": "S_ref - S_ours; positive means lower candidate CER"},
        "runs": runs, "provenance": {"cer": "measured_model"},
        "notApplicable": {"speakerAccuracy": "本协议是 ASR 训练，不是说话人分类任务",
                          "domainF1": "未提供真实领域分类任务与标签，不生成数值"},
        "limitations": ["结论仅适用于所选 ASR 模型、微调模式、预算和固定 E",
                        "CER 不做数字/日期口语化转换，双方统一使用 NFKC+小写+保留字母数字",
                        "未实现 B-WER/U-WER；专业术语应另行提供词表与人工复核",
                        "预训练模型可能接触公开语料，无法审计其训练污染",
                        "speaker/解码波形审计不等于所有近重复或领域分布已完成验证",
                        "多种子 std 反映训练随机性，不是总体泛化置信区间",
                        "公开子集 smoke/exploration 不构成真实业务数据质量验收"],
    }
