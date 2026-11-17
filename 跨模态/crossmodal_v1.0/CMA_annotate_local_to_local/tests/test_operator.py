"""算子 18 端到端业务测试（表1 #18 局部对局部）。

所有启用路线的用例都通过**明确标注的测试替身** ``StubEncoder`` 注入编码器，
用于验证分词→裁图→余弦→一对一→阈值分档→台账的规则正确性；
替身不代表 CLIP 语义精度。无权重场景另测显式失败/显式降级。
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from conftest import StubEncoder, box, record, write_png  # noqa: E402
from utils import backends  # noqa: E402
from utils import io as io_utils  # noqa: E402

import inference


@pytest.fixture(autouse=True)
def _force_no_model_weights(tmp_path, monkeypatch):
    """本用例集按「无权重环境」编写（显式降级/显式失败）；把 MODEL_ROOT 指向空目录，
    使降级断言在已挂载权重的机器上同样可复现。业务断言不变。"""
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "_no_model_weights"))
  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_encoder_cache():
    backends.clear_encoder_cache()
    yield
    backends.clear_encoder_cache()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def write_input(tmp_path, records, media=("media/shot_001.png",)):
    """把记录写成 JSONL，并在同目录准备被引用的真实 PNG。"""
    in_dir = tmp_path / "src"
    in_dir.mkdir(parents=True, exist_ok=True)
    for relative in media:
        write_png(in_dir / relative)
    lines = []
    for item in records:
        if isinstance(item, str):
            lines.append(item)
        else:
            lines.append(json.dumps(item, ensure_ascii=False))
    path = in_dir / "demo.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def read_jsonl(path) -> list:
    target = Path(path)
    if not target.exists():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run_op(input_path, tmp_path, *, overrides=None, encoder=None, unique_id="t1_f1",
           config_path=None):
    return inference.run(input_file=str(input_path), unique_id=unique_id,
                         config_path=str(config_path) if config_path else None,
                         output_root=str(tmp_path / "out"),
                         overrides=overrides, allow_external_input=True,
                         encoder=encoder)


ENABLED = {"enabled": True}


def happy_box_ids(count=3):
    return [box("b%d" % (i + 1), x=i * 10, y=i * 5, w=20, h=16) for i in range(count)]


def perfect_encoder(tokens, box_ids):
    return StubEncoder(text_angles={t: 0.0 for t in tokens},
                       box_angles={b: 0.0 for b in box_ids})


# --------------------------------------------------------------------------- #
# 1. 路线关闭
# --------------------------------------------------------------------------- #

def test_disabled_route_is_passthrough(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", regions=happy_box_ids())) + "\n",
                    encoding="utf-8")

    result = run_op(path, tmp_path)
    assert result["status"] == "success"
    assert result["entrypoint"] == "18_passthrough.jsonl"
    rows = read_jsonl(result["outputFile"])
    assert len(rows) == 1
    node = rows[0]["processing"]["18"]
    # [v4.2] status 双字段：int status + 原字符串迁入 process_status
    assert node["status"] == 1
    assert node["process_status"] == "skipped"
    assert node["route_enabled"] is False
    assert node["relations"] == []
    assert result["statistics"]["skipped_records"] == 1
    assert result["statistics"]["prefilled_records"] == 0
    assert result["statistics"]["sample_isolated"] == 0
    # 关闭时不产生媒体副本，也不写细粒度对应
    assert not (Path(result["runDir"]) / "media").exists()
    assert rows[0].get("phrase_box_relations") == []


# --------------------------------------------------------------------------- #
# 2. 开启：预填 / 待人工 / 阈值边界
# --------------------------------------------------------------------------- #

def test_enabled_prefills_and_awaits_confirmation(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬 草地",
                                      regions=happy_box_ids(2))) + "\n",
                    encoding="utf-8")

    encoder = perfect_encoder(["金毛犬", "草地"], ["b1", "b2"])
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)

    assert result["entrypoint"] == "18_phrase_region_annotations.jsonl"
    rows = read_jsonl(result["outputFile"])
    assert len(rows) == 1
    node = rows[0]["processing"]["18"]
    assert node["process_status"] == "prefilled"
    assert node["prefilled"] is True
    assert node["human_verified"] is False
    assert node["deliverable"] is False
    assert node["review_required"] is True
    assert node["min_clip_score"] == 0.25
    assert len(node["relations"]) >= 1
    for relation in node["relations"]:
        assert relation["human_verified"] is False
        assert relation["review_required"] is True
        assert relation["confirmation_method"] == "auto_prefill"
        # [v4.2] 词-区域关系记录同样 status 双字段
        assert relation["status"] == 1
        assert relation["process_status"] == "prefilled"
        assert relation["clip_score"] >= 0.25
        assert relation["box_id"]
        assert relation["phrase"]
    stats = result["statistics"]
    assert stats["prefilled_records"] == 1
    assert stats["relations_prefilled"] >= 1
    assert stats["deliverable_records"] == 0
    assert stats["human_verified_records"] == 0
    assert stats["conservation_ok"] is True
    # 媒体已搬进运行目录
    assert stats["media_carried"] == 1
    assert (Path(result["runDir"]) / "media" / "shot_001.png").is_file()


def test_below_threshold_all_pending_manual(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬",
                                      regions=happy_box_ids(1))) + "\n",
                    encoding="utf-8")

    encoder = StubEncoder(text_angles={"金毛犬": math.pi / 2},
                          box_angles={"b1": 0.0})
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)

    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["process_status"] == "pending_manual"
    assert node["prefilled"] is False
    assert all(r["process_status"] == "pending_manual" for r in node["relations"])
    assert all(r["confirmation_method"] == "pending_manual_review"
               for r in node["relations"])
    stats = result["statistics"]
    assert stats["pending_manual_records"] == 1
    assert stats["below_threshold"] == 1
    assert stats["prefilled_records"] == 0
    assert stats["sample_isolated"] == 0
    assert stats["conservation_ok"] is True
    # 待人工属于流程状态，样本同时进入人工台账
    assert read_jsonl(result["pendingFile"])


def test_threshold_boundary_equals_is_prefilled(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬",
                                      regions=happy_box_ids(1))) + "\n",
                    encoding="utf-8")
    encoder = StubEncoder(text_angles={"金毛犬": math.acos(0.25)},
                          box_angles={"b1": 0.0})
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["relations"][0]["clip_score"] == pytest.approx(0.25, abs=1e-6)
    assert node["prefilled"] is True


def test_threshold_override_is_honoured(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬",
                                      regions=happy_box_ids(1))) + "\n",
                    encoding="utf-8")
    encoder = StubEncoder(text_angles={"金毛犬": math.acos(0.30)},
                          box_angles={"b1": 0.0})
    low = run_op(path, tmp_path, overrides={"enabled": True, "min_clip_score": 0.25},
                 encoder=encoder, unique_id="t_low")
    high = run_op(path, tmp_path, overrides={"enabled": True, "min_clip_score": 0.35},
                  encoder=encoder, unique_id="t_high")
    assert read_jsonl(low["outputFile"])[0]["processing"]["18"]["prefilled"] is True
    assert read_jsonl(high["outputFile"])[0]["processing"]["18"]["prefilled"] is False


# --------------------------------------------------------------------------- #
# 3. 无对应关系 → 样本隔离库
# --------------------------------------------------------------------------- #

def test_filename_only_description_isolated(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="shot_001.png",
                                      regions=happy_box_ids(2))) + "\n",
                    encoding="utf-8")

    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    assert result["statistics"]["sample_isolated"] == 1
    assert result["statistics"]["isolated_no_correspondence"] == 1
    quarantined = read_jsonl(result["sampleQuarantineFile"])
    assert len(quarantined) == 1
    assert quarantined[0]["label"] == "样本-语义不通"
    assert quarantined[0]["destination"] == "样本隔离库"
    assert quarantined[0]["action"] == "isolate"
    # 隔离样本不进主输出
    assert read_jsonl(result["outputFile"]) == []
    assert result["statistics"]["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 4. 流程状态：留产线不隔离
# --------------------------------------------------------------------------- #

def test_missing_description_is_pending(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    payload = record("s1", text=None, regions=happy_box_ids(1))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    stats = result["statistics"]
    assert stats["missing_description"] == 1
    assert stats["pending_manual_records"] == 1
    assert stats["sample_isolated"] == 0
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["label"] == "流程-待补描述"
    assert node["action"] == "pending"


def test_no_boxes_is_pending_blank_list(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", regions=[])) + "\n", encoding="utf-8")

    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    stats = result["statistics"]
    assert stats["no_boxes"] == 1
    assert stats["sample_isolated"] == 0
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["relations"] == []
    assert node["label"] == "流程-无候选框"


def test_no_image_reference_is_pending(tmp_path):
    path = write_input(tmp_path, [record("s1", image=None, regions=happy_box_ids(1))])
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    stats = result["statistics"]
    assert stats["no_image_reference"] == 1
    assert stats["pending_manual_records"] == 1


# --------------------------------------------------------------------------- #
# 5. 原始隔离区
# --------------------------------------------------------------------------- #

def test_missing_image_file_is_origin(tmp_path):
    path = write_input(tmp_path, [record("s1", image="media/gone.png",
                                         regions=happy_box_ids(1))])
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    assert stats["sample_isolated"] == 0
    entries = read_jsonl(result["rejectsFile"])
    assert any(item["reason"] == "image_missing" for item in entries)
    assert entries[-1]["label"] == "原始文件-无法解析"


def test_illegal_image_path_is_origin(tmp_path):
    path = write_input(tmp_path, [record("s1", image="../../etc/passwd",
                                         regions=happy_box_ids(1))])
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    entries = read_jsonl(result["rejectsFile"])
    assert any(item["reason"] == "unsupported_media_path" for item in entries)


def test_all_crops_unreadable_is_origin(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬",
                                      regions=happy_box_ids(1))) + "\n",
                    encoding="utf-8")
    encoder = StubEncoder(crop_errors={"b1": "media_unreadable"})
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    entries = read_jsonl(result["rejectsFile"])
    assert any(item["reason"] == "image_unreadable" for item in entries)


def test_bad_boxes_are_dropped_not_fatal(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    boxes = [box("b1", x=1, y=1, w=20, h=16), {"box_id": "bad", "bbox": [0, 0, 0, 0]}]
    path.write_text(json.dumps(record("s1", text="金毛犬", regions=boxes)) + "\n",
                    encoding="utf-8")
    encoder = StubEncoder(text_angles={"金毛犬": 0.0}, box_angles={"b1": 0.0})
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)
    stats = result["statistics"]
    assert stats["boxes_received"] == 2
    assert stats["boxes_invalid"] == 1
    assert stats["boxes_used"] == 1
    assert stats["prefilled_records"] == 1


def test_all_boxes_invalid_becomes_pending(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬",
                                      regions=[{"bbox": [-1, 0, 0, 0]}])) + "\n",
                    encoding="utf-8")
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    stats = result["statistics"]
    assert stats["no_boxes"] == 1
    assert stats["boxes_invalid"] == 1


# --------------------------------------------------------------------------- #
# 6. 一对一匹配
# --------------------------------------------------------------------------- #

def test_one_to_one_more_words_than_boxes(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    payload = record("s1", text="金毛犬 草地 飞盘", tokens=["金毛犬", "草地", "飞盘"],
                     regions=happy_box_ids(2))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    encoder = perfect_encoder(["金毛犬", "草地", "飞盘"], ["b1", "b2"])
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["relations_total"] == 3
    assert node["relations_prefilled"] == 2
    assert node["relations_pending"] == 1
    assigned = [r["box_id"] for r in node["relations"] if r["box_id"]]
    assert sorted(assigned) == sorted(set(assigned))          # 框不被重复使用
    without = [r for r in node["relations"] if r["box_id"] is None]
    assert without and without[0]["reason"] == "no_available_region"


def test_upstream_tokens_are_reused(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    payload = record("s1", text="完全无关的说明文字", tokens=["金毛犬"],
                     regions=happy_box_ids(1))
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    result = run_op(path, tmp_path, overrides=ENABLED,
                    encoder=perfect_encoder(["金毛犬"], ["b1"]))
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["tokenizer_source"] == "pretrained"
    assert node["relations"][0]["phrase"] == "金毛犬"


# --------------------------------------------------------------------------- #
# 7. 模型不可用：显式失败或显式降级（绝不伪造分数）
# --------------------------------------------------------------------------- #

def test_model_unavailable_strict_raises(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬",
                                      regions=happy_box_ids(1))) + "\n",
                    encoding="utf-8")
    with pytest.raises(inference.ModelUnavailable):
        run_op(path, tmp_path, overrides=ENABLED, encoder=None)


def test_model_unavailable_degraded_has_no_scores(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬",
                                      regions=happy_box_ids(1))) + "\n",
                    encoding="utf-8")
    result = run_op(path, tmp_path,
                    overrides={"enabled": True, "allow_degraded": True},
                    encoder=None)
    assert result["status"] == "degraded"
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["process_status"] == "degraded"
    assert node["degraded"] is True
    assert node["model"] is None
    assert all(r["clip_score"] is None for r in node["relations"]) or node["relations"] == []
    assert result["statistics"]["model_unavailable"] == 1
    assert result["model"] is None
    assert result["statistics"]["conservation_ok"] is True


def test_score_error_is_pending_not_fabricated(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(record("s1", text="金毛犬",
                                      regions=happy_box_ids(1))) + "\n",
                    encoding="utf-8")
    encoder = StubEncoder(fail="cuda oom")
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["process_status"] == "pending_manual"
    assert node["reason"] == "score_error"
    assert all(r["clip_score"] is None for r in node["relations"])
    assert result["statistics"]["score_error_records"] == 1


# --------------------------------------------------------------------------- #
# 8. 读入层异常
# --------------------------------------------------------------------------- #

def test_bad_lines_blank_lines_and_missing_id(tmp_path):
    good = json.dumps(record("s1", regions=happy_box_ids(1)), ensure_ascii=False)
    lines = [good, "{不是合法 JSON", "", '{"text": "x"}', "[1, 2, 3]"]
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    stats = result["statistics"]
    # 坏行 = 非法 JSON(1) + 非对象(1)；缺 sample_id 属原始隔离而非坏行
    assert stats["bad_lines"] == 2
    assert stats["blank_lines"] == 1
    assert stats["input_records"] == 4
    assert stats["origin_quarantined"] == 1
    assert stats["conservation_ok"] is True
    reasons = {item["reason"] for item in read_jsonl(result["rejectsFile"])}
    assert "json_parse_error" in reasons
    assert "record_not_object" in reasons
    assert "missing_field: sample_id" in reasons


def test_duplicate_identifier_is_origin(tmp_path):
    payload = json.dumps(record("s1", regions=happy_box_ids(1)), ensure_ascii=False)
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    path.write_text(payload + "\n" + payload + "\n", encoding="utf-8")
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    assert result["statistics"]["origin_quarantined"] == 1
    assert any(item["reason"] == "duplicate_identifier"
               for item in read_jsonl(result["rejectsFile"]))


def test_encoding_error_is_recorded(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    path = in_dir / "demo.jsonl"
    path.write_bytes('{"sample_id": "中文"}\n'.encode("gbk"))
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    stats = result["statistics"]
    assert stats["encoding_error"] is True
    assert stats["file_read_incomplete"] is True
    assert stats["bad_lines"] >= 1
    assert stats["conservation_ok"] is True


def test_empty_input_file(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    path = in_dir / "demo.jsonl"
    path.write_text("", encoding="utf-8")
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 9. 任务隔离、血缘与原子性
# --------------------------------------------------------------------------- #

def test_two_unique_ids_are_isolated(tmp_path):
    path = write_input(tmp_path, [record("s1", regions=happy_box_ids(1))])
    first = run_op(path, tmp_path, overrides=ENABLED,
                   encoder=perfect_encoder([], []), unique_id="task_a")
    second = run_op(path, tmp_path, overrides=ENABLED,
                    encoder=perfect_encoder([], []), unique_id="task_b")
    assert first["runDir"] != second["runDir"]
    assert Path(first["runDir"]).is_dir() and Path(second["runDir"]).is_dir()
    assert "task_a" in first["runDir"] and "task_b" in second["runDir"]
    # 同名 run 目录互不覆盖
    assert Path(first["runDir"]).parent.parent.name == "task_a"
    again = run_op(path, tmp_path, overrides=ENABLED,
                   encoder=perfect_encoder([], []), unique_id="task_a")
    assert again["runId"] != first["runId"]


def test_invalid_unique_id_rejected(tmp_path):
    path = write_input(tmp_path, [record("s1", regions=happy_box_ids(1))])
    with pytest.raises(ValueError):
        run_op(path, tmp_path, unique_id="../escape")


def test_input_file_is_not_truncated(tmp_path):
    in_dir = tmp_path / "src"
    in_dir.mkdir()
    write_png(in_dir / "media" / "shot_001.png")
    path = in_dir / "demo.jsonl"
    original = json.dumps(record("s1", regions=happy_box_ids(1))) + "\n"
    path.write_text(original, encoding="utf-8")
    run_op(path, tmp_path, overrides=ENABLED, encoder=perfect_encoder([], []))
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.skip(reason="v4.2 血缘输出已停用：inference 不再产出血缘文件")
def test_lineage_contains_required_fields(tmp_path):
    path = write_input(tmp_path, [record("s1", regions=happy_box_ids(1))])
    result = run_op(path, tmp_path, overrides=ENABLED,
                    encoder=perfect_encoder([], []), unique_id="lin_f1")
    record_line = json.loads(Path(result["lineageFile"]).read_text(encoding="utf-8"))[0]
    for field in ("operator", "operator_version", "uniqueId", "stageId", "inputFile",
                  "outputFile", "status", "startedAt", "finishedAt", "config",
                  "statistics", "artifacts", "formalOperatorNumber"):
        assert field in record_line, field
    assert record_line["formalOperatorNumber"] == 18
    assert record_line["stageId"] == "S3"
    assert record_line["statistics"]["input_records"] == 1
    snapshot = record_line["config"]["business"]
    assert snapshot["enabled"] is True
    assert snapshot["min_clip_score"] == 0.25
    assert snapshot["clip_model"] == "CLIP-ViT/B-32"


@pytest.mark.skip(reason="v4.2 血缘输出已停用：中间/阶段血缘形态函数不再被接口层使用")
def test_intermediate_and_stage_shapes(tmp_path):
    path = write_input(tmp_path, [record("s1", regions=happy_box_ids(1))])
    result = run_op(path, tmp_path, overrides=ENABLED,
                    encoder=perfect_encoder([], []), unique_id="shape_f1")
    intermediate = inference.build_intermediate_lineage(result)
    assert intermediate["outputFile"] == result["outputFile"]
    assert intermediate["uniqueId"] == "shape_f1"
    stage = inference.build_stage_payload(result)
    assert isinstance(stage, list) and stage
    assert "outputFile" not in stage[0]
    assert stage[0]["operator"] == "phrase_region_annotation"


def test_output_is_utf8(tmp_path):
    path = write_input(tmp_path, [record("s1", regions=happy_box_ids(1))])
    result = run_op(path, tmp_path, overrides=ENABLED,
                    encoder=perfect_encoder([], []))
    raw = Path(result["outputFile"]).read_bytes()
    raw.decode("utf-8")            # 不抛异常即为严格 UTF-8
    text = Path(result["outputFile"]).read_text(encoding="utf-8")
    assert "金毛犬" in text


def test_statistics_keys_present(tmp_path):
    path = write_input(tmp_path, [record("s1", regions=happy_box_ids(1))])
    result = run_op(path, tmp_path, overrides=ENABLED,
                    encoder=perfect_encoder([], []))
    stats = result["statistics"]
    for key in ("input_records", "valid_records", "bad_lines", "prefilled_records",
                "pending_manual_records", "skipped_records", "sample_isolated",
                "origin_quarantined", "scored_records", "relations_total",
                "relations_prefilled", "relations_pending", "boxes_received",
                "boxes_used", "boxes_invalid", "crops_scored", "media_carried",
                "deliverable_records", "human_verified_records",
                "conservation_ok", "conservation_failures"):
        assert key in stats, key


# --------------------------------------------------------------------------- #
# 无图视频：按关键帧裁图打分（v1.0.1）
# --------------------------------------------------------------------------- #

def test_video_frame_boxes_are_scored_per_frame(tmp_path):
    """#17 下发的帧框按各自 frame_path 裁图打分；不再因「没有图像」停在 no_boxes。"""
    in_dir = tmp_path / "src"
    (in_dir / "media" / "vid").mkdir(parents=True)
    write_png(in_dir / "media" / "vid" / "frame_000001.png")
    write_png(in_dir / "media" / "vid" / "frame_000002.png")

    payload = record("vid-1", text="操作员 检查 设备",
                     tokens=["操作员", "检查", "设备"], image=None)
    payload["processing"] = {"17": {
        "path": "media/vid/silent.mp4",
        "video_frames": [{"path": "media/vid/frame_000001.png", "timestamp": 0.0},
                         {"path": "media/vid/frame_000002.png", "timestamp": 1.0}],
        "regions": [dict(box("b1", x=2, y=2), frame_path="media/vid/frame_000001.png"),
                    dict(box("b2", x=40, y=2), frame_path="media/vid/frame_000002.png")]}}
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    encoder = StubEncoder(text_angles={"操作员": 0.0, "检查": 0.0, "设备": 0.0},
                          box_angles={"b1": 0.0, "b2": 0.0})
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)

    stats = result["statistics"]
    assert stats["prefilled_records"] == 1
    assert stats["no_boxes"] == 0
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["image_source"] == "processing.17.video_frames.path"
    assert node["frame_scoped_boxes"] == 2
    assert node["crop_sources"] == 2
    assert node["crops_scored"] == 2 and node["crops_failed"] == 0
    assert node["relations_prefilled"] >= 1
    assert node["human_verified"] is False
    # 两帧各编码一次（按帧分组，同一帧的多个框只打开一次）
    assert len(encoder.crop_calls) == 2


def test_video_frame_missing_file_counts_as_crop_failure(tmp_path):
    in_dir = tmp_path / "src"
    (in_dir / "media" / "vid").mkdir(parents=True)
    write_png(in_dir / "media" / "vid" / "frame_000001.png")
    payload = record("vid-2", text="操作员 检查 设备",
                     tokens=["操作员", "检查", "设备"], image=None)
    payload["processing"] = {"17": {
        "path": "media/vid/silent.mp4",
        "regions": [dict(box("b1"), frame_path="media/vid/frame_000001.png"),
                    dict(box("b2", x=40), frame_path="media/vid/absent.png")]}}
    path = in_dir / "demo.jsonl"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    result = run_op(path, tmp_path, overrides=ENABLED,
                    encoder=StubEncoder(text_angles={"操作员": 0.0},
                                        box_angles={"b1": 0.0, "b2": 0.0}))
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["crops_scored"] == 1
    assert node["crops_failed"] == 1
    assert result["statistics"]["origin_quarantined"] == 0


# --------------------------------------------------------------------------- #
# 名词短语变体与裁图选项（v1.0.1 效果优化）
# --------------------------------------------------------------------------- #

def test_noun_phrase_variant_drives_prefill(tmp_path):
    """词本身分数不够、但它所在的名词短语分数够 → 仍可预填（word 仍是匹配单元）。"""
    path = write_input(tmp_path, [record("s1", text="红色小汽车",
                                         regions=[box("b1", label="car")])])
    encoder = StubEncoder(text_angles={"红色": math.pi / 2,
                                       "小汽车": math.pi / 2,
                                       "红色小汽车": 0.0},
                          box_angles={"b1": 0.0})
    result = run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)

    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["process_status"] == "prefilled"
    prefilled = [r for r in node["relations"]
                 if r["process_status"] == "prefilled"]
    assert prefilled, "短语变体应产生预填关系"
    assert prefilled[0]["phrase"] in ("红色", "小汽车")
    assert prefilled[0]["matched_text"] == "红色小汽车"
    assert prefilled[0]["variant"] == "noun_phrase"
    assert prefilled[0]["phrase_tokens"] == ["红色", "小汽车"]
    assert prefilled[0]["clip_score"] >= 0.25
    # 统计与溯源
    assert node["phrases_total"] == 1
    assert node["phrases_used"] >= 1
    assert node["token_variants"] == 3
    stats = result["statistics"]
    assert stats["phrases_total"] == 1
    assert stats["phrases_used"] >= 1
    assert stats["token_variants"] == 3
    assert stats["merge_phrases"] is True
    assert stats["crop_padding_ratio"] == 0.0
    assert stats["background_suppression"] == "none"
    # 按类别记录校准证据（不改变 0.25 阈值）
    profile = node["class_profile"]["car"]
    assert profile["relations"] >= 1
    assert profile["best_score"] == pytest.approx(1.0)


def test_merge_phrases_can_be_disabled(tmp_path):
    path = write_input(tmp_path, [record("s1", text="红色小汽车",
                                         regions=[box("b1")])])
    encoder = StubEncoder(text_angles={"红色": math.pi / 2,
                                       "小汽车": math.pi / 2,
                                       "红色小汽车": 0.0},
                          box_angles={"b1": 0.0})
    result = run_op(path, tmp_path,
                    overrides={"enabled": True, "merge_phrases": False},
                    encoder=encoder)
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["process_status"] == "pending_manual"
    assert node["phrases_total"] == 0
    assert all(r["matched_text"] == r["phrase"] for r in node["relations"])
    assert all(r["variant"] == "word" for r in node["relations"])


def test_crop_options_are_forwarded_to_encoder(tmp_path):
    path = write_input(tmp_path, [record("s1", text="金毛犬",
                                         regions=happy_box_ids(1))])
    encoder = perfect_encoder(["金毛犬"], ["b1"])
    result = run_op(path, tmp_path,
                    overrides={"enabled": True,
                               "crop": {"padding_ratio": 0.05,
                                        "background_suppression": "mean"}},
                    encoder=encoder)
    assert encoder.crop_options_calls == [{"padding_ratio": 0.05,
                                           "background_suppression": "mean"}]
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    assert node["crop_padding_ratio"] == pytest.approx(0.05)
    assert node["background_suppression"] == "mean"


def test_default_crop_options_keep_legacy_encoder_call(tmp_path):
    path = write_input(tmp_path, [record("s1", text="金毛犬",
                                         regions=happy_box_ids(1))])
    encoder = perfect_encoder(["金毛犬"], ["b1"])
    run_op(path, tmp_path, overrides=ENABLED, encoder=encoder)
    assert encoder.crop_options_calls == [None]


def test_crop_config_validation(config_file):
    from utils.schemas import ConfigError

    with pytest.raises(ConfigError):
        inference.load_config(str(config_file), {"crop": {"padding_ratio": 0.5}})
    with pytest.raises(ConfigError):
        inference.load_config(str(config_file),
                              {"crop": {"background_suppression": "mean"}})
    with pytest.raises(ConfigError):
        inference.load_config(str(config_file),
                              {"crop": {"padding_ratio": 0.1,
                                        "background_suppression": "blur"}})
    config = inference.load_config(
        str(config_file),
        {"crop": {"padding_ratio": 0.1, "background_suppression": "mean"}})
    assert config.crop_snapshot() == {"padding_ratio": 0.1,
                                      "background_suppression": "mean"}
    assert config.business_snapshot()["merge_phrases"] is True


def test_class_calibration_changes_assignment_not_scores(tmp_path):
    """按类别校准只改变「词→框」指派：原始分数与 0.25 阈值判定不变。"""
    path = write_input(tmp_path, [record("s1", text="人 猫",
                                         regions=[box("b1", label="person"),
                                                  box("b2", label="person"),
                                                  box("b3", label="cat")])])
    encoder = StubEncoder(text_angles={"人": 0.0, "猫": 0.9},
                          box_angles={"b1": 0.3, "b2": 0.35, "b3": 1.5})
    raw = run_op(path, tmp_path,
                 overrides={"enabled": True, "class_calibration": "none",
                            "merge_phrases": False},
                 encoder=encoder)
    node_raw = read_jsonl(raw["outputFile"])[0]["processing"]["18"]
    raw_targets = {r["phrase"]: r["class_name"] for r in node_raw["relations"]}

    calibrated = run_op(path, tmp_path,
                        overrides={"enabled": True, "class_calibration": "znorm",
                                   "merge_phrases": False},
                        encoder=encoder)
    node_cal = read_jsonl(calibrated["outputFile"])[0]["processing"]["18"]
    cal_targets = {r["phrase"]: r["class_name"] for r in node_cal["relations"]}

    # 未校准：猫 被高分 person 框抢走；校准后：猫 回到 cat 框
    assert raw_targets["猫"] == "person"
    assert cal_targets["猫"] == "cat"
    assert cal_targets["人"] == "person"
    # 原始余弦不因校准改变：未校准 0.8525（person 框），校准后 0.8253（cat 框）
    raw_cat = [r for r in node_raw["relations"] if r["phrase"] == "猫"][0]
    cal_cat = [r for r in node_cal["relations"] if r["phrase"] == "猫"][0]
    assert raw_cat["clip_score"] == pytest.approx(math.cos(0.9 - 0.35))
    assert cal_cat["clip_score"] == pytest.approx(math.cos(0.9 - 1.5))
    # 校准分单独输出，判定仍看 clip_score
    cat_rel = [r for r in node_cal["relations"] if r["phrase"] == "猫"][0]
    assert cat_rel["assignment_score"] is not None
    assert cat_rel["assignment_score"] != cat_rel["clip_score"]
    assert node_cal["class_calibration"]["method"] == "znorm"
    assert node_cal["class_profile"]["cat"]["calibration"]["method"] == "znorm"
    assert calibrated["statistics"]["calibrated_records"] >= 1


def test_class_prior_changes_assignment_only(tmp_path):
    """类别名先验把「词说的类别」作为二次排序项：只改指派，不改 clip_score/阈值。"""
    path = write_input(tmp_path, [record("s1", text="人 猫",
                                         regions=[box("b1", label="person"),
                                                  box("b2", label="person"),
                                                  box("b3", label="cat")])])
    encoder = StubEncoder(text_angles={"人": 0.1, "猫": 1.0,
                                       "person": 0.1, "cat": 1.0},
                          box_angles={"b1": 0.0, "b2": 0.5, "b3": 1.5})
    result = run_op(path, tmp_path,
                    overrides={"enabled": True, "class_calibration": "prior",
                               "class_prior_weight": 0.05, "merge_phrases": False},
                    encoder=encoder)
    node = read_jsonl(result["outputFile"])[0]["processing"]["18"]
    targets = {r["phrase"]: r["class_name"] for r in node["relations"]}
    assert targets["人"] == "person"
    assert targets["猫"] == "cat"          # 原始 b2/b3 同分，先验把 猫 分到 cat 框
    cat_rel = [r for r in node["relations"] if r["phrase"] == "猫"][0]
    assert cat_rel["clip_score"] == pytest.approx(math.cos(1.0 - 1.5))   # 原始余弦不变
    assert cat_rel["assignment_score"] > cat_rel["clip_score"]
    assert node["class_calibration"]["method"] == "prior"
    assert node["class_calibration"]["weight"] == pytest.approx(0.05)
    assert node["class_calibration"]["classes"] == 2
    assert result["statistics"]["calibrated_records"] >= 1


def test_class_calibration_config_validation(config_file):
    from utils.schemas import ConfigError

    with pytest.raises(ConfigError):
        inference.load_config(str(config_file), {"class_calibration": "per_class"})
    config = inference.load_config(str(config_file),
                                   {"class_calibration": "none"})
    assert config.class_calibration == "none"
    assert config.business_snapshot()["class_calibration"] == "none"
    prior = inference.load_config(str(config_file), {"class_calibration": "prior"})
    assert prior.business_snapshot()["class_prior_weight"] == pytest.approx(0.05)
    for bad in (-0.1, 0.6, "abc", True):
        with pytest.raises(ConfigError):
            inference.load_config(str(config_file), {"class_prior_weight": bad})
