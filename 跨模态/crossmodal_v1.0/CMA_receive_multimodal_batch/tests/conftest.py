"""算子 01「接入多模态批次」测试夹具。

设计原则（与产线其它算子一致）：

- **不下载任何外部数据**：所有输入与媒体字节都在 ``tmp_path`` 内确定性构造，
  只用 Python 标准库（``struct``/``zlib`` 均不依赖第三方）；
- **业务参数字典与实现同源**：``biz()`` 直接调用
  ``utils.intake.default_business``，避免测试与实现各写一套默认值；
- **样例媒体字节确定性**：``MEDIA_BUILDERS`` 生成带真实文件头（PNG/JPEG/WEBP/
  WAV/MP3/FLAC/MP4/MOV/AVI）的最小字节串，用于验证「文件打不开/头签名不符」路径。
"""
from __future__ import annotations
import tempfile
import os

import json
import struct
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pytest

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

CONFIG_PATH = BASE_DIR / "configs" / "config.yaml"

#: 表1 #01 冻结来源（中文标签）
SOURCE_PLATFORM_LABEL = "采集平台批次"
SOURCE_TEXT_LABEL = "预训练文本产线交付"
SOURCE_IMAGE_LABEL = "预训练图像产线交付"
SOURCE_AUDIO_LABEL = "预训练音频产线交付"
SOURCE_VIDEO_LABEL = "预训练视频产线交付"

DEFAULT_SOURCE = SOURCE_IMAGE_LABEL
DEFAULT_SOURCE_ID = "SRC-IMG-2026"
DEFAULT_BATCH = "BATCH-20261001-01"
DEFAULT_VERSION = "pipeline-1.4.2"

#: 上传/输入入口名（批次登记清单）
ENTRY_NAME = "01_batch_manifest.jsonl"


# --------------------------------------------------------------------------- #
# 确定性媒体字节
# --------------------------------------------------------------------------- #

def png_bytes(payload: bytes = b"opaque") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + payload


def jpeg_bytes(payload: bytes = b"opaque") -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + payload


def webp_bytes(payload: bytes = b"opaque") -> bytes:
    chunk = b"WEBP" + payload
    return b"RIFF" + struct.pack("<I", len(chunk)) + chunk


def wav_bytes(payload: bytes = b"\x00" * 32) -> bytes:
    chunk = b"WAVEfmt " + payload
    return b"RIFF" + struct.pack("<I", len(chunk)) + chunk


def avi_bytes(payload: bytes = b"opaque") -> bytes:
    chunk = b"AVI " + payload
    return b"RIFF" + struct.pack("<I", len(chunk)) + chunk


def mp3_bytes(payload: bytes = b"opaque") -> bytes:
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + payload


def flac_bytes(payload: bytes = b"opaque") -> bytes:
    return b"fLaC" + payload


def mp4_bytes(payload: bytes = b"isom") -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42" + payload


def mov_bytes(payload: bytes = b"qt  ") -> bytes:
    return b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + payload


def text_bytes(payload: bytes = b"hello world") -> bytes:
    return payload + b"\n"


MEDIA_BUILDERS = {
    "png": png_bytes,
    "jpg": jpeg_bytes,
    "webp": webp_bytes,
    "wav": wav_bytes,
    "avi": avi_bytes,
    "mp3": mp3_bytes,
    "flac": flac_bytes,
    "mp4": mp4_bytes,
    "mov": mov_bytes,
    "txt": text_bytes,
    "json": lambda payload=b'{"a": 1}': payload,
    "csv": lambda payload=b"a,b": payload,
}


def media_bytes(ext: str) -> bytes:
    return MEDIA_BUILDERS[ext]()


def write_media(directory: Path, relative: str, ext: str) -> Path:
    """在 ``directory`` 下写入一个带真实文件头的 ``relative`` 文件。"""
    target = Path(directory) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(media_bytes(ext))
    return target


# --------------------------------------------------------------------------- #
# 输入记录构造
# --------------------------------------------------------------------------- #

def make_record(sample_id: Any = "s-0001", *, files: Optional[List[Dict[str, Any]]] = None,
                ext: str = "jpg", modality: Optional[str] = None,
                link: Optional[str] = None, remote: bool = True,
                source: Any = DEFAULT_SOURCE, source_id: Any = DEFAULT_SOURCE_ID,
                batch_id: Any = DEFAULT_BATCH, upstream_version: Any = DEFAULT_VERSION,
                license: Any = "可训练",
                extra: Optional[Dict[str, Any]] = None,
                drop: Sequence[str] = ()) -> Dict[str, Any]:
    """构造一条批次登记记录（默认：一张远端图像 + 完整登记字段 + 可训练）。"""
    if files is None:
        if link is None:
            link = ("oss://bucket/obj/%s.%s" % (sample_id, ext) if remote
                    else "media/%s.%s" % (sample_id, ext))
        entry: Dict[str, Any] = {"oss_uri": link} if remote else {"path": link}
        if modality is not None:
            entry["modality"] = modality
        files = [entry]
    record: Dict[str, Any] = {
        "sample_id": sample_id,
        "files": files,
        "source": source,
        "source_id": source_id,
        "batch_id": batch_id,
        "upstream_version": upstream_version,
        "license": license,
    }
    if extra:
        record.update(extra)
    for key in drop:
        record.pop(key, None)
    return record


def evaluate(record: Dict[str, Any], *, biz: Dict[str, Any], lineno: int = 1,
             local_dirs: Optional[Sequence[Path]] = None, sample_id: Optional[str] = None):
    """便捷入口：展开记录并评估第一个对象（单文件记录）。"""
    from utils import intake

    objects = intake.expand_objects(record)
    return intake.evaluate_object(
        record, sample_id=sample_id if sample_id is not None else record.get("sample_id", ""),
        obj=objects[0], business=biz, lineno=lineno, local_dirs=list(local_dirs or []))


def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    """把记录（dict 或原始字符串行）写成 UTF-8 JSONL。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    for item in rows:
        lines.append(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_input_dir(root: Path, rows: Iterable[Any], *, unique_id: str = "prev_task",
                    entry_name: str = ENTRY_NAME,
                    media: Optional[Dict[str, str]] = None) -> Path:
    """在 ``OUTPUT_DIR/<uniqueId>/input`` 下装配入口文件（含运行清单）。"""
    from utils import transport

    input_dir = Path(root) / unique_id / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    for relative, ext in (media or {}).items():
        write_media(input_dir, relative, ext)
    entry = write_jsonl(input_dir / entry_name, rows)
    manifest = transport.build_manifest(
        entry_name, [{"path": entry_name, "role": "batch_manifest"}])
    transport.write_manifest(input_dir, manifest)
    return entry


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not Path(path).is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture()
def biz() -> Dict[str, Any]:
    """与部署配置同源的业务参数字典（默认总开关开启）。"""
    from utils import intake

    return intake.default_business()


@pytest.fixture()
def mixed_rows() -> List[Any]:
    """一次覆盖：通过（远端/本地）、格式非法、授权非法、缺登记、重复、缺 id、坏行。"""
    return [
        make_record("m-pass-01"),
        make_record("m-pass-02", ext="wav", modality="audio",
                    source=SOURCE_AUDIO_LABEL, source_id="SRC-AUD-2026"),
        make_record("m-pass-local-01", remote=False, ext="png"),
        make_record("m-format-01", ext="gif"),
        make_record("m-license-01", license="不可训练"),
        make_record("m-license-02", license="待确认"),
        make_record("m-missing-01", drop=("batch_id",)),
        make_record("m-no-id-01", extra=None, drop=("sample_id",)),
        make_record("m-dup-01"),
        make_record("m-dup-01"),
        "{坏掉的一行",
        "[]",
    ]


@pytest.fixture()
def whitelist_config(tmp_path) -> str:
    path = tmp_path / "whitelist.yaml"
    path.write_text("file_check_mode: whitelist\n", encoding="utf-8")
    return str(path)


@pytest.fixture()
def strict_config(tmp_path) -> str:
    path = tmp_path / "strict.yaml"
    path.write_text("file_check_mode: strict\n", encoding="utf-8")
    return str(path)


@pytest.fixture()
def invalid_mode_config(tmp_path) -> str:
    path = tmp_path / "invalid.yaml"
    path.write_text("file_check_mode: turbo\n", encoding="utf-8")
    return str(path)


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
