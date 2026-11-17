"""#16 测试共享夹具：配置、样例构造、确定性编码器替身、临时任务目录。

``StubEncoder`` 只是**测试替身**：它给出确定性的向量，由算子自身的
``cosine_similarity`` 算出指定余弦值，用来验证"人工改描述必须用同一实例重算"
这条调用链。它不参与任何交付结论，公开入口也绝不会用它。
"""
from __future__ import annotations
import tempfile
import os

import copy
import json
import math
import sys
from pathlib import Path

import pytest

OPERATOR_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = OPERATOR_DIR / "dataset" / "sample"
for extra in (str(OPERATOR_DIR), str(SAMPLE_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import make_sample  # noqa: E402  (样例构造器复用，避免测试与样例口径漂移)
from utils import alignment as al  # noqa: E402
from utils import backends, io as op_io  # noqa: E402


@pytest.fixture()
def operator_dir() -> Path:
    return OPERATOR_DIR


@pytest.fixture()
def fresh_cfg() -> dict:
    """经过 validate_config 的默认配置（每个测试独立深拷贝，互不污染）。"""
    import inference

    return copy.deepcopy(al.validate_config(inference.load_config(None)))


@pytest.fixture()
def raw_cfg() -> dict:
    import inference

    return copy.deepcopy(inference.load_config(None))


class StubEncoder(object):
    """确定性编码器替身：文本向量与图像向量的真实余弦等于 ``score``。"""

    def __init__(self, score: float = 0.40, name: str = "CLIP-ViT/B-32",
                 version: str = "ViT-B-32") -> None:
        self.score = max(-1.0, min(1.0, float(score)))
        self.name = name
        self.version = version
        self.text_calls = 0
        self.image_calls = 0
        self.image_paths: list = []

    def _text_vector(self):
        remainder = math.sqrt(max(0.0, 1.0 - self.score ** 2))
        return [self.score, remainder] + [0.0] * 6

    def encode_texts(self, texts):
        self.text_calls += 1
        assert texts, "重评分必须传入人工改写后的描述"
        return [list(self._text_vector()) for _ in texts]

    def encode_images(self, paths):
        self.image_calls += 1
        self.image_paths.extend([str(item) for item in paths])
        return [[1.0] + [0.0] * 7 for _ in paths], [None] * len(paths)

    def model_identity(self) -> dict:
        return {"name": self.name, "version": self.version, "library": "stub",
                "weights": None, "device": "cpu", "degraded": False,
                "real_backend": False, "untrained_architecture_only": False,
                "score_semantics": "cosine"}


class FailingEncoder(StubEncoder):
    """编码失败替身：用于验证"推理失败"路径不产生任何分数。"""

    def encode_texts(self, texts):
        raise backends.BackendUnavailable("测试替身：模拟推理失败")


@pytest.fixture()
def stub_encoder():
    def factory(score: float = 0.40) -> StubEncoder:
        return StubEncoder(score=score)

    return factory


@pytest.fixture()
def build_pair():
    """复用样例构造器生成候选对，保证测试与样例字段口径一致。"""
    return make_sample.pair


@pytest.fixture()
def media(tmp_path) -> Path:
    """在 tmp_path 下写入真实可解码的 PNG，与 plan 夹具共用同一目录。"""
    make_sample.write_png(tmp_path / "media" / "cat.png", 8, 6, (200, 160, 120))
    make_sample.write_png(tmp_path / "media" / "street.png", 8, 6, (120, 160, 200))
    return tmp_path


@pytest.fixture()
def write_jsonl(tmp_path):
    def writer(name: str, items, raw_lines=None) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = list(raw_lines or [])
        lines.extend(json.dumps(item, ensure_ascii=False) for item in items)
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path

    return writer


@pytest.fixture()
def plan(tmp_path, fresh_cfg):
    """便捷入口：把候选对（或原始行）跑一次 plan_alignment。"""
    def runner(records=None, *, raw_lines=None, encoder=None, events=None,
               cfg=None, now="2026-10-07T10:00:00+08:00",
               limit_isolation=None, limit_auto=None) -> al.AlignmentResult:
        items = list(records or [])
        path = tmp_path / "input.jsonl"
        lines = list(raw_lines or [])
        lines.extend(json.dumps(item, ensure_ascii=False) for item in items)
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        used_cfg = copy.deepcopy(cfg or fresh_cfg)
        if limit_isolation is not None or limit_auto is not None:
            section = used_cfg[al.SECTION]
            if limit_isolation is not None:
                section["isolation_threshold"] = limit_isolation
            if limit_auto is not None:
                section["auto_align_threshold"] = limit_auto
        return al.plan_alignment(
            op_io.read_jsonl(path), used_cfg, unique_id="test_task",
            media_root=tmp_path, now=now, encoder=encoder, events=events)

    return runner


def decisions_of(result: al.AlignmentResult) -> dict:
    """主清单 + 隔离台账 → {pair_id: alignment.whole}（不对齐样本只出现在隔离台账）。"""
    found = {record["pair_id"]: record["alignment"]["whole"] for record in result.main}
    for entry in result.isolation:
        record = entry.get("record") or {}
        if record.get("pair_id"):
            found[record["pair_id"]] = record["alignment"]["whole"]
    return found


# --- MTY 归档/入库隔离（导入期设置，pytest 不在算子目录留下 _mty_transport） ---
# 必须是导入期而不是夹具：个别算子的 session 级夹具会先启动 uvicorn 子进程，
# 子进程启动时即继承环境变量；夹具顺序无法保证「先设变量、后起子进程」。
if not os.environ.get("MTY_TRANSPORT_DIR"):
    os.environ["MTY_TRANSPORT_DIR"] = tempfile.mkdtemp(prefix="mty_transport_")
