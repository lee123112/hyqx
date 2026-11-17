"""#14 时序路线边界测试（2026-11-05 修复）。

背景：时序样本自带 ``timeline`` / ``temporal_bindings`` / ``temporal_anchors``，
它们是按窗口切出来的片段，整窗口的 CLIP 视频-文本语义分天然偏低。修复前这些样本
被 #14 按 ``segment_below_threshold`` 隔离，而同一窗口 #15 判 0.0s 偏差通过——
两个算子结论互相矛盾，且时序样本进不了交付。

修复后：记录带时序结构时 #14 判 ``not_applicable``（不评分、不隔离、不消耗模型），
时序结论由 #15 给出。``temporal_route_exclusion=false`` 只用于回归对比。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fixtures import (build_input_tree, default_frames, default_media,  # noqa: E402
                      make_config, make_flat_record, StubEncoder)
from utils import frames as frames_mod  # noqa: E402
from utils import video_text as vt  # noqa: E402


def prepare(tmp_path, media=None, name="input"):
    base = build_input_tree(tmp_path / name, [], media=media or default_media())
    return [base]


def spec_of(record, media_roots):
    spec = vt.resolve_pair(record)
    spec["record"] = record
    return spec


def temporal_record(sample_id: str, description: str, frames=None, **extra):
    record = make_flat_record(sample_id, description, frames or default_frames(2))
    record.update({
        "timeline": {"video": [{"id": "s0", "timestamp_sec": 0.05}],
                     "audio": [{"id": "s0", "start_sec": 0.05, "end_sec": 1.55},
                               {"id": "s1", "start_sec": 1.55, "end_sec": 3.0}],
                     "text": [{"id": "s0", "start_sec": 0.05, "end_sec": 1.55,
                               "content": "挥手"},
                              {"id": "s1", "start_sec": 1.55, "end_sec": 3.0,
                               "content": "转身"}]},
        "temporal_bindings": [{"text": "先挥手", "start": 0.0, "end": 1.5},
                              {"text": "再转身", "start": 1.5, "end": 3.0}],
        "temporal_anchors": [{"text": "转身", "time": 2.0}],
    })
    record.update(extra)
    return record


# --------------------------------------------------------------------------- #
# 结构识别
# --------------------------------------------------------------------------- #

def test_plain_record_has_no_temporal_evidence():
    record = make_flat_record("s-00", "描述", default_frames(2))
    assert vt.temporal_route_evidence(record) is None
    for key in vt.TEMPORAL_STRUCTURE_KEYS:
        item = make_flat_record("s-%s" % key, "描述", default_frames(2))
        item[key] = []
        assert vt.temporal_route_evidence(item) is None


def test_temporal_evidence_counts_structure():
    evidence = vt.temporal_route_evidence(temporal_record("s-01", "描述"))
    assert evidence is not None
    assert set(evidence["declared_by"]) == {"timeline", "temporal_bindings",
                                            "temporal_anchors"}
    assert evidence["timeline_tracks"] == 3      # video/audio/text 三条轨
    assert evidence["timeline_items"] == 5       # 1 + 2 + 2
    assert evidence["bindings"] == 2
    assert evidence["anchors"] == 1


def test_temporal_evidence_tolerates_partial_structure():
    record = make_flat_record("s-02", "描述", default_frames(2))
    record["temporal_bindings"] = [{"text": "x", "start": 0.0, "end": 1.0}]
    evidence = vt.temporal_route_evidence(record)
    assert evidence["declared_by"] == ["temporal_bindings"]
    assert evidence["timeline_items"] == 0 and evidence["anchors"] == 0


# --------------------------------------------------------------------------- #
# 判定与分流
# --------------------------------------------------------------------------- #

def test_temporal_record_is_not_applicable_and_never_scores(tmp_path):
    roots = prepare(tmp_path)
    encoder = StubEncoder(default=0.05)
    record = temporal_record("s-03", "描述")
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=encoder,
                                 event_time="2026-11-05T00:00:00+08:00")
    assert outcome["action"] == vt.ACTION_NOT_APPLICABLE
    assert outcome["reason"] == vt.REASON_TEMPORAL_ROUTE
    node = outcome["node"]
    assert node["process_status"] == vt.STATUS_NOT_APPLICABLE
    assert node["status"] == vt.status_int(vt.STATUS_NOT_APPLICABLE) == 1
    assert node["passed"] is None
    assert node["video_text_score"] is None and node["segment_mean"] is None
    assert node["route"] == "temporal"
    assert node["destination"] == vt.DEST_PIPELINE
    assert node["action"] == "passthrough"
    assert node["temporal_route_evidence"]["bindings"] == 2
    # 不加载、不调用模型
    assert encoder.text_calls == [] and encoder.image_calls == []
    entry = outcome["entry"]
    assert entry["reason"] == vt.REASON_TEMPORAL_ROUTE
    assert entry["label"] == vt.LABEL_FLOW_TEMPORAL_ROUTE


def test_temporal_exclusion_can_be_disabled_for_regression(tmp_path):
    """temporal_route_exclusion=false 复现旧行为：按时序窗口的语义分隔离。"""
    roots = prepare(tmp_path)
    outcome = vt.evaluate_record(
        spec_of(temporal_record("s-04", "描述"), roots), 1, media_roots=roots,
        config=make_config(temporal_route_exclusion=False),
        encoder=StubEncoder(default=0.05),
        event_time="2026-11-05T00:00:00+08:00")
    assert outcome["action"] == vt.ACTION_ISOLATE
    assert outcome["reason"] == vt.REASON_SEGMENT_BELOW


def test_temporal_exclusion_runs_before_structure_checks(tmp_path):
    """时序判定先于"缺帧/缺描述"：结构缺失不影响边界结论。"""
    roots = prepare(tmp_path)
    record = temporal_record("s-05", "描述", frames=[])
    record["text"] = None
    record["metadata"] = {}
    outcome = vt.evaluate_record(spec_of(record, roots), 1, media_roots=roots,
                                 config=make_config(), encoder=StubEncoder(),
                                 event_time="2026-11-05T00:00:00+08:00")
    assert outcome["reason"] == vt.REASON_TEMPORAL_ROUTE


def test_not_applicable_node_shape():
    node = vt.not_applicable_node()
    assert node["process_status"] == vt.STATUS_NOT_APPLICABLE
    assert node["process_status"] == vt.STATUS_NOT_APPLICABLE
    assert node["status"] == 1
    assert node["scored_at"] is None
    assert node["video_text_score"] is None
    assert node["low_score_threshold"] == vt.DEFAULT_THRESHOLD
    assert node["max_frames_per_segment"] == frames_mod.MAX_FRAMES_PER_SEGMENT


def test_not_applicable_status_conserves_as_reported_bucket():
    """not_applicable 与 passed/skipped 一样是"已报告"状态，不参与隔离计数。"""
    assert vt._STATUS_AS_INT[vt.STATUS_NOT_APPLICABLE] == 1
    assert vt._STATUS_AS_INT[vt.STATUS_ISOLATED] == 0


def test_config_defaults_and_domain():
    config = make_config()
    assert config.temporal_route_exclusion is True
    with pytest.raises(Exception):
        make_config(temporal_route_exclusion="false")
