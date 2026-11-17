"""算子 23 测试公共设施：路径引导、配置夹具、上游记录与材料夹具。

诚实边界：
- 本文件只构造**上游算子的字段结构**（#22 Token 样本清单、#20 质量报告、
  #24 抽检结论、#21 隔离清单、版本说明），形状来自 `跨模态数据流说明`
  与本目录 `DEVELOPMENT_PLAN.md`，不加载任何模型、不联网、不下载外部数据；
- 算子 23 为**系统功能型**算子（表1 模型列 = ``--``），没有模型替身，
  全部断言都是确定性规则断言，不涉及任何精度宣称；
- 媒体文件用标准库（``zlib``/``struct``/``wave``）确定性合成，可被真实解码器打开。
"""
from __future__ import annotations
import tempfile
import os

import json
import struct
import sys
import wave
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pytest

BASE = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(BASE), str(TESTS_DIR), str(BASE / "utils")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# --------------------------------------------------------------------------- #
# 媒体合成（标准库，确定性）
# --------------------------------------------------------------------------- #

def png_bytes(width: int = 8, height: int = 8, color: Sequence[int] = (200, 30, 30)) -> bytes:
    """生成真实可解码的最小 PNG（8bit RGB，无滤波）。"""
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def wav_bytes(frames: int = 800, sample_rate: int = 16000, amplitude: int = 3000) -> bytes:
    """生成真实可解码的 16kHz 单声道 WAV（交替正负样本，非静音）。"""
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        payload = bytearray()
        for index in range(frames):
            value = amplitude if index % 2 == 0 else -amplitude
            payload += struct.pack("<h", value)
        handle.writeframes(bytes(payload))
    return buffer.getvalue()


def write_media(root: Path, relative: str, payload: bytes) -> Path:
    target = Path(root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


# --------------------------------------------------------------------------- #
# 上游记录构造（#22 Token 样本清单）
# --------------------------------------------------------------------------- #

def token_record(sample_id: Optional[str] = "cm-0001", *, route: str = "image_text",
                 modalities: Optional[Sequence[str]] = None,
                 media: Optional[Sequence[str]] = None,
                 token: Optional[str] = "<image> 一只橘猫坐在窗台上 <image>",
                 external: Optional[Sequence[str]] = None,
                 isolation: Optional[Dict[str, Any]] = None,
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一条 #22 形态的 Token 记录。"""
    modal = list(modalities) if modalities is not None else ["image", "text"]
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "batch_id": "batch-A",
        "scene_id": "scene-01",
        "modalities": modal,
        "routes": [route],
        "token": token,
        "text": {"content": "一只橘猫坐在窗台上", "description_source": "caption"},
        "metadata": {"license": "可训练", "source": "batch-A"},
        "processing": {"22": {"operator": "#22", "status": "tokenized",
                              "route": route, "markers": ["<image>"]}},
    }
    if media:
        record["media"] = [{"path": item, "role": "image"} for item in media]
    if external:
        record["media"] = list(record.get("media") or []) + [
            {"path": item, "role": "image"} for item in external]
    if isolation:
        record["processing"]["21"] = isolation
    if extra:
        record = deep_merge(record, extra)
    return record


def isolation_marker(sample_id: str = "cm-0011", *, reason_code: str = "score_below_threshold",
                     status: str = "isolated") -> Dict[str, Any]:
    """#21 处置错配样本写入的隔离标记（两种写法：isolated=true / status=isolated）。"""
    return {"operator": "mismatch_disposal", "status": status, "isolated": True,
            "reason_code": reason_code, "label": "样本-语义不通",
            "action": "isolate", "destination": "sample_isolation_library",
            "sample_id": sample_id, "event_time": "2026-10-08T10:00:00+08:00",
            "retry_count": 2}


def deep_merge(*parts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """按顺序深合并字典；后者覆盖前者的标量，字典逐键递归合并。"""
    merged: Dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = deep_merge(merged[key], value)
            else:
                merged[key] = value
    return merged


# --------------------------------------------------------------------------- #
# 材料构造（#20 质量报告 / #24 抽检结论 / #21 隔离清单 / 版本说明）
# --------------------------------------------------------------------------- #

def quality_report(*, written_back: bool = False, operator: str = "alignment_qc") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "format": "crossmodal-quality-report-v1",
        "operator": operator,
        "operator_version": "v1.0.0",
        "report_id": "qr-0001",
        "checks": {"openable": True, "min_modalities": True},
        "statistics": {"input": 16, "output": 10},
    }
    if written_back:
        payload["status"] = "written_back"
        payload["writeback"] = {"completed": True, "completedAt": "2026-10-08T11:00:00+08:00",
                                "operator": "#24"}
        payload["version_state"] = "deliverable"
    else:
        payload["status"] = "waiting_writeback"
        payload["writeback"] = {"completed": False}
        payload["version_state"] = "pending_release"
    return payload


def sampling_conclusion(*, written_back: bool = False, sampling_rate: float = 0.1
                        ) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "format": "crossmodal-sampling-conclusion-v1",
        "operator": "#24",
        "sampling_rate": sampling_rate,
        "layers": {"image_text": 10, "audio_text": 3},
        "conclusion": {"judged_wrong": 1, "passed": 12},
    }
    payload["writeback"] = ({"completed": True, "completedAt": "2026-10-08T11:30:00+08:00"}
                            if written_back else {"completed": False})
    payload["status"] = "written_back" if written_back else "waiting_writeback"
    return payload


def version_notes(*, routes: Optional[Sequence[str]] = ("image_text",),
                  granularity: Optional[str] = "whole",
                  version: str = "v1.0.0") -> Dict[str, Any]:
    payload: Dict[str, Any] = {"format": "crossmodal-version-notes-v1",
                               "dataset_version": version,
                               "created_at": "2026-10-08",
                               "notes": "demo 版本说明"}
    if routes is not None:
        payload["enabled_routes"] = list(routes)
    if granularity is not None:
        payload["annotation_granularity"] = granularity
    return payload


def isolation_entry(sample_id: str, *, label: str = "样本-语义不通",
                    reason_code: str = "score_below_threshold") -> Dict[str, Any]:
    return {"sample_id": sample_id, "label": label, "stage": "S4",
            "operator": "#21", "action": "isolate",
            "destination": "sample_isolation_library", "reason_code": reason_code,
            "event_time": "2026-10-08T10:00:00+08:00", "batch_id": "batch-A",
            "retry_count": 2}


# --------------------------------------------------------------------------- #
# 输入包构造
# --------------------------------------------------------------------------- #

def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    """写出 JSONL；字符串元素按原文写出（用于构造坏行）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
             for row in rows]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_json(path: Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target


DEFAULT_MEDIA = ("media/cm-0001.png", "media/cm-0002.png", "media/cm-0003.wav",
                 "media/cm-0004.png")


def build_input_package(root: Path, *, records: Sequence[Any],
                        materials: Optional[Dict[str, Any]] = None,
                        media: Sequence[str] = DEFAULT_MEDIA,
                        entrypoint: str = "samples.jsonl",
                        write_manifest: bool = True,
                        material_dir: bool = True) -> Path:
    """构造一个任务输入包目录，返回入口 JSONL 的绝对路径。

    ``materials`` 支持 ``quality_report`` / ``sampling_conclusion`` /
    ``isolation_list`` / ``version_notes`` 四个键，值为待写入的负载
    （``isolation_list`` 为记录列表，其余为字典）。
    """
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    entry = write_jsonl(base / entrypoint, records)

    for relative in media:
        suffix = Path(relative).suffix.lower()
        payload = wav_bytes() if suffix == ".wav" else png_bytes()
        write_media(base, relative, payload)

    material_payload = {key: value for key, value in (materials or {}).items()
                        if value is not None}
    material_root = (base / "materials") if material_dir else base
    material_root.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    if "quality_report" in material_payload:
        write_json(material_root / "quality_report.json", material_payload["quality_report"])
        written.append("materials/quality_report.json")
    if "sampling_conclusion" in material_payload:
        write_json(material_root / "sampling_conclusion.json",
                   material_payload["sampling_conclusion"])
        written.append("materials/sampling_conclusion.json")
    if "isolation_list" in material_payload:
        write_jsonl(material_root / "isolation_list.jsonl", material_payload["isolation_list"])
        written.append("materials/isolation_list.jsonl")
    if "version_notes" in material_payload:
        write_json(material_root / "version_notes.json", material_payload["version_notes"])
        written.append("materials/version_notes.json")

    if write_manifest:
        manifest = {"format": "crossmodal-manifest-v1", "entrypoint": entrypoint,
                    "artifacts": [{"path": entrypoint, "role": "records"}]
                    + [{"path": item, "role": "sidecar"} for item in written]
                    + [{"path": item, "role": "media"} for item in media]}
        write_json(base / "manifest.json", manifest)
    return entry


def build_scenario(root: Path, *, written_back: bool = False,
                   include_version_notes: bool = True, records: Optional[Sequence[Any]] = None
                   ) -> Path:
    """构造一个「待发布 / 可交付」两态可控的完整输入包。"""
    rows = list(records if records is not None else DEFAULT_RECORDS)
    materials: Dict[str, Any] = {
        "quality_report": quality_report(written_back=written_back),
        "sampling_conclusion": sampling_conclusion(written_back=written_back),
        "isolation_list": [isolation_entry("cm-0013")],
    }
    if include_version_notes:
        materials["version_notes"] = version_notes()
    return build_input_package(root, records=rows, materials=materials)


#: 覆盖全部业务分支的默认记录集（10 可交付 + 3 隔离 + 3 原始隔离）
DEFAULT_RECORDS: Sequence[Any] = (
    token_record("cm-0001", media=["media/cm-0001.png"]),
    token_record("cm-0002", media=["media/cm-0002.png"]),
    token_record("cm-0003", route="audio_text", modalities=["audio", "text"],
                 token="<audio> 一段播报 <audio>", media=["media/cm-0003.wav"]),
    token_record("cm-0004", route="video_text", modalities=["video", "text"],
                 token="<video> 一段街景 <video>", media=["media/cm-0004.png"]),
    token_record("cm-0005", media=["media/cm-9999.png"]),
    token_record("cm-0006", modalities=["image", "video", "text"],
                 token="<image> 猫 <image> <video> 同场景 <video>",
                 media=["media/cm-0001.png"]),
    token_record("cm-0007", external=["oss://bucket/demo/cm-0007.png"]),
    token_record("cm-0008", media=["media/cm-0002.png"]),
    token_record("cm-0009", route="video_text", modalities=["video", "text"],
                 token="<video> 路口车流 <video>", media=["media/cm-0004.png"]),
    token_record("cm-0010", media=["media/cm-0001.png"]),
    token_record("cm-0011", media=["media/cm-0002.png"],
                 isolation=isolation_marker("cm-0011", status="isolated")),
    token_record("cm-0012", media=["media/cm-0002.png"],
                 isolation=isolation_marker("cm-0012", status="quarantined")),
    token_record("cm-0013", media=["media/cm-0002.png"]),
    {"batch_id": "batch-A", "modalities": ["image", "text"], "routes": ["image_text"],
     "token": "<image> 缺标识 <image>", "text": {"content": "缺标识"}},
    token_record("cm-0015", media=["C:/temp/escape.png"]),
    token_record("cm-0016", media=["media/cm-0001.png"], token=""),
    '{"sample_id": "cm-bad-1", ',
    '["not", "an", "object"]',
    "",
)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG_YAML = """\
operator:
  number: 23
  name: delivery_package
  label: 压缩交付对齐语料包
  version: v1.0.0
  enabled: true
stage_id: S6
enabled: true
archive_format: zip
exclude_isolated: true
status_before_review: 待发布
status_after_review: 可交付
include_media: true
require_deliverable: false
transport:
  format: crossmodal-files-v1
  max_response_bytes: 67108864
"""


@pytest.fixture()
def config_file(tmp_path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return path


def config_variant(base_config: Path, extra_yaml: str, name: str = "config_variant.yaml") -> Path:
    """基于默认配置再叠加一段 YAML 覆盖，用于配置边界测试。"""
    path = Path(base_config).with_name(name)
    path.write_text(DEFAULT_CONFIG_YAML + extra_yaml, encoding="utf-8")
    return path


@pytest.fixture()
def output_root(tmp_path) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def scenario_root(tmp_path) -> Path:
    """默认「材料齐全但报告/抽检未回写」的输入包（应为待发布）。"""
    return build_scenario(tmp_path / "task_pending", written_back=False)


def build_input_zip(tmp_path: Path, name: str = "task.zip", *,
                    written_back: bool = True,
                    include_version_notes: bool = True,
                    records: Optional[Sequence[Any]] = None,
                    prefix: str = "") -> Path:
    """把完整输入包（入口 + 清单 + 材料 + 媒体）打成上传用 zip。"""
    import zipfile

    source = build_scenario(tmp_path / ("zip_src_" + name.replace(".zip", "")),
                            written_back=written_back,
                            include_version_notes=include_version_notes,
                            records=records)
    root = source.parent
    target = tmp_path / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                bundle.write(path, prefix + path.relative_to(root).as_posix())
    return target


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
