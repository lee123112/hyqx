"""测试共用的 fixture 构造：直接复用 dataset/sample/make_sample.py，避免两套样例漂移。"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parents[1]
#: 上游算子 id → 预置的阶段血缘节点（与上游实际 ID 对齐）
UPSTREAM_NODES = [
    ("text_cleaning", "03_text_cleaned.jsonl"),
    ("image_standardization", "04_image_index.jsonl"),
    ("video_keyframe_extraction", "05_frame_index.jsonl"),
    ("audio_normalization", "06_audio_index.jsonl"),
]


def load_make_sample():
    """按路径加载生成脚本（dataset/sample 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        "cm07_make_sample", BASE_DIR / "dataset" / "sample" / "make_sample.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_fixture(destination: Path) -> Path:
    """用生成脚本产出一套完整 fixture（demo.jsonl + media + manifest + 旁路）。"""
    load_make_sample().generate(destination)
    return destination


def stage_lineage_records(unique_id: str = "cm07_task_0001") -> List[Dict[str, Any]]:
    """预置 03—06 四条阶段血缘节点，用于验证阶段末返回五条。"""
    records: List[Dict[str, Any]] = []
    for index, (operator, output) in enumerate(UPSTREAM_NODES, start=1):
        records.append({
            "operator": operator,
            "version": "v1.0.0",
            "uniqueId": unique_id,
            "stageId": "S2",
            "runId": "run%03d" % index,
            "inputFile": "%s/input/%s" % (unique_id, output if index == 1 else "prev.jsonl"),
            "outputFile": "%s/%s/run%03d/%s" % (unique_id, operator, index, output),
            "status": "success",
            "startedAt": "2026-10-02T00:00:0%dZ" % index,
            "finishedAt": "2026-10-02T00:00:1%dZ" % index,
            "config": {"business": {}, "deployment": {}, "overridden_keys": []},
            "statistics": {"input": 5, "output": 5, "rejected": 0},
        })
    return records


def write_stage_context(directory: Path, unique_id: str = "cm07_task_0001",
                        prior: Optional[List[Dict[str, Any]]] = None,
                        stage: Optional[List[Dict[str, Any]]] = None) -> Path:
    payload = {
        "stageId": "S2",
        "uniqueId": unique_id,
        "runId": "run004",
        "entrypoint": "06_audio_index.jsonl",
        "stage_lineage": stage if stage is not None else stage_lineage_records(unique_id),
        "prior_lineage": prior or [],
    }
    path = directory / "stage_context.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def make_record(sample_id: str, **overrides: Any) -> Dict[str, Any]:
    """构造一条最小完整记录（默认 text+image）。"""
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "text": {"content": "hello world", "original": "hello world",
                 "tokens": ["hello", "world"], "token_offsets": [[0, 5], [6, 11]],
                 "lang": "en"},
        "image": {"path": "04_images/%s.png" % sample_id, "width": 64, "height": 64,
                  "format": "png", "blur_score": 100.0},
        "video": None,
        "audio": None,
        "processing": {"03": {"status": "success", "reason": None, "metrics": {}}},
    }
    record.update(overrides)
    return record


def make_av_record(sample_id: str, video_duration: float = 1.0,
                   audio_duration: float = 1.0,
                   video_frames: Optional[List[Dict[str, Any]]] = None,
                   audio_segments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """构造 video+audio 记录（text=null）。"""
    return {
        "sample_id": sample_id,
        "text": None,
        "image": None,
        "video": {
            "path": "media/original/%s.mp4" % sample_id,
            "duration": video_duration,
            "fps": 25.0,
            "frames": video_frames if video_frames is not None else [],
        },
        "audio": {
            "path": "06_audio/%s.wav" % sample_id,
            "sample_rate": 16000,
            "channels": 1,
            "duration": audio_duration,
            "segments": audio_segments if audio_segments is not None else [],
        },
    }


def write_task(task_root: Path, records: List[Any], entrypoint: str = "demo.jsonl",
               media: Optional[Dict[str, bytes]] = None,
               manifest: bool = True,
               sidecars: Optional[Dict[str, bytes]] = None) -> Path:
    """写一个上传态任务目录：``task_root/input/<entrypoint>`` + 媒体 + manifest。"""
    input_dir = task_root / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        if isinstance(record, str):
            lines.append(record)
        else:
            lines.append(json.dumps(record, ensure_ascii=False))
    (input_dir / entrypoint).write_text("\n".join(lines) + "\n", encoding="utf-8")

    artifacts = [{"path": entrypoint, "role": "main"}]
    for relative, payload in (media or {}).items():
        target = input_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        artifacts.append({"path": relative, "role": "media"})
    for name, payload in (sidecars or {}).items():
        (input_dir / name).write_bytes(payload)
        artifacts.append({"path": name, "role": "sidecar"})
    if manifest:
        (input_dir / "manifest.json").write_text(json.dumps({
            "format": "crossmodal-manifest-v1",
            "entrypoint": entrypoint,
            "artifacts": artifacts,
            "input_source": "fixture",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return input_dir


PNG_BYTES = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt "


def upload_dir(output_root: Path, unique_id: str = "cm07_unit") -> Path:
    """任务根内的上传目录 ``OUTPUT_DIR/<uniqueId>/input``（模拟 /uploadFile 解包结果）。"""
    target = output_root / unique_id / "input"
    target.mkdir(parents=True, exist_ok=True)
    return target


def copy_sample_into(target_input_dir: Path, source: Optional[Path] = None) -> Path:
    """把生成好的 fixture 复制为任务上传目录内容。"""
    origin = source or build_fixture(target_input_dir.parent.parent / "generated_sample")
    if target_input_dir.exists():
        shutil.rmtree(target_input_dir)
    shutil.copytree(origin, target_input_dir)
    return target_input_dir


def upstream_dir(output_root: Path, unique_id: str = "cm07_unit",
                 operator: str = "audio_normalization", run_id: str = "run001") -> Path:
    """同 uniqueId 的上游运行目录（阶段内串联时的 inputFile 来源）。"""
    target = output_root / unique_id / operator / run_id
    target.mkdir(parents=True, exist_ok=True)
    return target
