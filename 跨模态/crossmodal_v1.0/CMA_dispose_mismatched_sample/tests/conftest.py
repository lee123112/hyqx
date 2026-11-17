"""算子 21 测试公共夹具：路径、临时输出根、样例自愈与合成记录工厂。

- 所有测试只写入 pytest 的 ``tmp_path``，不污染算子目录；
- 样例缺失时本地重建（``dataset/sample/make_sample.py``：纯标准库、无随机数、不联网）；
- 合成记录工厂同时支持「上游台账条目」（扁平）与「完整记录 + 嵌套 disposition」两种输入形态。
"""
from __future__ import annotations
import tempfile
import os

import json
import sys
import zipfile
from pathlib import Path

import pytest

OPERATOR_DIR = Path(__file__).resolve().parents[1]
if str(OPERATOR_DIR) not in sys.path:
    sys.path.insert(0, str(OPERATOR_DIR))

SAMPLE_DIR = OPERATOR_DIR / "dataset" / "sample"
ENTRYPOINT = "demo.jsonl"


@pytest.fixture(scope="session", autouse=True)
def _ensure_sample() -> None:
    entry = SAMPLE_DIR / ENTRYPOINT
    manifest = SAMPLE_DIR / "manifest.json"
    if entry.is_file() and manifest.is_file():
        return
    import runpy

    runpy.run_path(str(SAMPLE_DIR / "make_sample.py"), run_name="__main__")
    if not entry.is_file():
        raise RuntimeError("样例生成失败：%s" % entry)


@pytest.fixture()
def output_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "output"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUTPUT_DIR", str(root))
    return root


@pytest.fixture()
def sample_dir() -> Path:
    return SAMPLE_DIR


@pytest.fixture()
def sample_entry() -> Path:
    return SAMPLE_DIR / ENTRYPOINT


@pytest.fixture()
def sample_media_dir() -> Path:
    return SAMPLE_DIR / "media"


@pytest.fixture()
def bundle_zip(tmp_path, sample_dir) -> Path:
    """把 ``dataset/sample/`` 打成上传用 zip（含入口、manifest 与 media）。"""
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(sample_dir.rglob("*")):
            if not path.is_file() or path.name == "make_sample.py":
                continue
            handle.write(path, path.relative_to(sample_dir).as_posix())
    return archive


# ------------------------------------------------------------------ 记录工厂
def make_entry(sample_id, *, label=None, action=None, destination=None, retry_count=None,
               stage=None, upstream_operator=None, event_time=None, batch_id=None,
               requires_human=None, review_required=None, image=None, audio=None,
               video=None, upstream_return_count=None, record_extra=None,
               extra=None) -> dict:
    """构造一条「上游台账条目」（扁平结构，与 #10/#13/#14/#17/#18 的旁路清单一致）。"""
    entry = {"sample_id": sample_id}
    for key, value in (("label", label), ("action", action), ("destination", destination),
                       ("stage", stage), ("operator", upstream_operator),
                       ("event_time", event_time), ("batch_id", batch_id)):
        if value is not None:
            entry[key] = value
    if retry_count is not None:
        entry["retry_count"] = retry_count
    if requires_human is not None:
        entry["requires_human_confirmation"] = bool(requires_human)
    if review_required is not None:
        entry["review_required"] = bool(review_required)

    record = {}
    if image is not None:
        record["image"] = {"path": image}
    if audio is not None:
        record["audio"] = {"path": audio}
    if video is not None:
        record["video"] = {"path": video}
    if upstream_return_count is not None:
        record["operator_21"] = {"return_count": int(upstream_return_count)}
    if record_extra:
        record.update(record_extra)
    if record:
        entry["record"] = record
    if extra:
        entry.update(extra)
    return entry


def make_full_record(sample_id, *, action, label, destination=None, retry_count=0,
                     batch_id="BATCH-21", requires_human=None, image=None) -> dict:
    """构造「完整样本记录 + 嵌套 disposition」形态（校验嵌套兜底链）。"""
    record = {
        "sample_id": sample_id,
        "disposition": {"label": label, "action": action, "destination": destination,
                        "retry_count": retry_count},
        "metadata": {"batch_id": batch_id},
    }
    if requires_human is not None:
        record["metadata"]["human_review_required"] = bool(requires_human)
    if image is not None:
        record["image"] = {"path": image}
    return record


def write_jsonl(path, records, *, raw_lines=None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    if raw_lines:
        lines.extend(raw_lines)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def read_jsonl(path) -> list:
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
