"""``utils.frames`` 纯逻辑测试：帧归一化、语义分段、**整段视频合计**帧预算与均匀抽帧。

口径（2026-10-23）：``max_frames_per_segment`` 是整段视频合计上限，多段视频把预算
分配到各段（段数 ≤ 上限时每段至少 1 帧；段数 > 上限时按段均匀覆盖、部分段 0 帧）。

本文件不加载任何模型、不访问网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from utils import frames as frames_mod  # noqa: E402


# --------------------------------------------------------------------------- #
# 常量与上限
# --------------------------------------------------------------------------- #

def test_frame_limits_match_table1():
    """表1：整段视频合计最多 8 帧（配置键范围 1–8）。"""
    assert frames_mod.MAX_FRAMES_PER_SEGMENT == 8
    assert frames_mod.MAX_FRAMES_MIN == 1
    assert frames_mod.MAX_FRAMES_MAX == 8


def test_clamp_limit_enforces_table1_range():
    assert frames_mod.clamp_limit(0) == 1
    assert frames_mod.clamp_limit(-3) == 1
    assert frames_mod.clamp_limit(1) == 1
    assert frames_mod.clamp_limit(5) == 5
    assert frames_mod.clamp_limit(8) == 8
    assert frames_mod.clamp_limit(99) == 8


# --------------------------------------------------------------------------- #
# resolve_frames
# --------------------------------------------------------------------------- #

def test_resolve_frames_normalizes_and_keeps_order():
    payload = {
        "frames": [
            {"frame_index": 1, "timestamp": 0.0, "path": "frames/a.png"},
            {"timestamp": "1.5", "path": "frames/b.png"},
            {"path": "  frames/c.png  "},
        ]
    }
    resolved = frames_mod.resolve_frames(payload)
    assert [item["path"] for item in resolved] == [
        "frames/a.png", "frames/b.png", "frames/c.png"]
    assert resolved[0]["timestamp"] == 0.0
    assert resolved[1]["timestamp"] == 1.5
    assert resolved[1]["frame_index"] is None
    assert resolved[2]["timestamp"] is None


def test_resolve_frames_drops_entries_without_path():
    payload = {"frames": [{"timestamp": 0.0}, "frames/x.png", {"path": ""}, {"path": None}]}
    assert frames_mod.resolve_frames(payload) == []


@pytest.mark.parametrize("payload", [None, {}, {"frames": None}, {"frames": "nope"}, []])
def test_resolve_frames_handles_missing_payload(payload):
    assert frames_mod.resolve_frames(payload) == []


def test_resolve_frames_parses_string_form_video():
    """``video`` 允许写成纯路径字符串；此时没有帧。"""
    assert frames_mod.resolve_frames("video/clip.mp4") == []


# --------------------------------------------------------------------------- #
# resolve_segments
# --------------------------------------------------------------------------- #

def test_resolve_segments_without_segments_is_single_whole_video_segment():
    payload = {"frames": [{"path": "frames/a.png", "timestamp": 0.0}]}
    segments = frames_mod.resolve_segments(payload, "描述", "text.content")
    assert len(segments) == 1
    assert segments[0]["index"] == 0
    assert segments[0]["start"] is None and segments[0]["end"] is None
    assert segments[0]["text"] == "描述"
    assert segments[0]["text_source"] == "text.content"
    assert segments[0]["frames"] is None


def test_resolve_segments_uses_per_segment_text_override():
    payload = {
        "frames": [],
        "segments": [
            {"start": 0.0, "end": 2.0, "text": "第一段描述"},
            {"start": 2.0, "end": 4.0, "transcript": "第二段描述"},
            {"start": 4.0, "end": 6.0},
        ],
    }
    segments = frames_mod.resolve_segments(payload, "整段描述", "text.content")
    assert [item["text"] for item in segments] == ["第一段描述", "第二段描述", "整段描述"]
    assert segments[0]["text_source"] == "video.segments[0].text"
    assert segments[1]["text_source"] == "video.segments[1].transcript"
    assert segments[2]["text_source"] == "text.content"
    assert segments[2]["start"] == 4.0 and segments[2]["end"] == 6.0


def test_resolve_segments_supports_segment_own_frames():
    payload = {
        "frames": [{"path": "frames/whole.png", "timestamp": 0.0}],
        "segments": [{"start": 0.0, "end": 1.0,
                      "frames": [{"path": "frames/seg.png", "timestamp": 0.5}]}],
    }
    segments = frames_mod.resolve_segments(payload, "描述", None)
    assert [item["path"] for item in segments[0]["frames"]] == ["frames/seg.png"]


def test_resolve_segments_skips_non_dict_entries():
    payload = {"segments": ["bad", {"start": 0.0, "end": 1.0}, 42]}
    segments = frames_mod.resolve_segments(payload, "描述", None)
    assert len(segments) == 1
    assert segments[0]["index"] == 0


# --------------------------------------------------------------------------- #
# 抽帧与均匀采样
# --------------------------------------------------------------------------- #

def test_evenly_spaced_returns_all_when_under_limit():
    items = [1, 2, 3]
    assert frames_mod.evenly_spaced(items, 5) == [1, 2, 3]
    assert frames_mod.evenly_spaced(items, 3) == [1, 2, 3]


def test_evenly_spaced_picks_unique_items_preserving_order():
    items = list(range(12))
    picked = frames_mod.evenly_spaced(items, 8)
    assert len(picked) == 8
    assert picked == sorted(picked)
    assert len(set(picked)) == 8
    assert picked[0] == 0 and picked[-1] == 11


def test_evenly_spaced_single_limit_picks_middle():
    items = list(range(5))
    assert frames_mod.evenly_spaced(items, 1) == [2]


@pytest.mark.parametrize("limit", [0, -1])
def test_evenly_spaced_non_positive_limit(limit):
    assert frames_mod.evenly_spaced([1, 2, 3], limit) == []


def test_select_frames_filters_by_time_range():
    candidates = [
        {"path": "f0.png", "timestamp": 0.0},
        {"path": "f1.png", "timestamp": 1.0},
        {"path": "f2.png", "timestamp": 2.0},
        {"path": "f3.png", "timestamp": 3.0},
    ]
    selected = frames_mod.select_frames(candidates, 0.0, 2.0, 8)
    assert [item["path"] for item in selected] == ["f0.png", "f1.png"]


def test_select_frames_without_timestamps_uses_all_frames():
    candidates = [{"path": "f%d.png" % index, "timestamp": None} for index in range(4)]
    selected = frames_mod.select_frames(candidates, 0.0, 2.0, 8)
    assert len(selected) == 4


def test_select_frames_falls_back_to_all_when_range_empty():
    """时间戳与分段区间不重叠时回退整段候选，绝不产出空结果掩盖问题。"""
    candidates = [{"path": "f%d.png" % index, "timestamp": 10.0 + index} for index in range(3)]
    selected = frames_mod.select_frames(candidates, 0.0, 1.0, 8)
    assert len(selected) == 3


def test_select_frames_enforces_eight_frame_cap():
    candidates = [{"path": "f%02d.png" % index, "timestamp": float(index)}
                  for index in range(30)]
    selected = frames_mod.select_frames(candidates, 0.0, 30.0, 100)
    assert len(selected) == frames_mod.MAX_FRAMES_MAX


def test_select_frames_respects_smaller_limit():
    candidates = [{"path": "f%02d.png" % index, "timestamp": float(index)}
                  for index in range(10)]
    assert len(frames_mod.select_frames(candidates, 0.0, 10.0, 3)) == 3
    assert len(frames_mod.select_frames(candidates, 0.0, 10.0, 1)) == 1


def test_segment_frame_count_reports_selection():
    """``segment_frames`` 是分段到帧的唯一定口，供业务模块与统计复用。"""
    payload = {
        "frames": [{"path": "f%d.png" % index, "timestamp": float(index)}
                   for index in range(6)],
        "segments": [{"start": 0.0, "end": 3.0}, {"start": 3.0, "end": 6.0}],
    }
    segments = frames_mod.resolve_segments(payload, "描述", None)
    picked = frames_mod.segment_frames(payload, segments, limit=8)
    assert [len(group) for group in picked] == [3, 3]
    assert [item["path"] for item in picked[0]] == ["f0.png", "f1.png", "f2.png"]
    assert [item["path"] for item in picked[1]] == ["f3.png", "f4.png", "f5.png"]


def test_segment_frames_enforces_whole_video_cap():
    """单语义段：整段视频合计上限 = 8 帧（与旧口径等价）。"""
    payload = {
        "frames": [{"path": "f%02d.png" % index, "timestamp": float(index)}
                   for index in range(20)],
    }
    segments = frames_mod.resolve_segments(payload, "描述", None)
    picked = frames_mod.segment_frames(payload, segments, limit=8)
    assert len(picked) == 1
    assert len(picked[0]) == 8


# --------------------------------------------------------------------------- #
# 整段视频帧预算分配（2026-10-23 口径）
# --------------------------------------------------------------------------- #

def _segments(count, per_segment=10):
    return [{"start": float(index * per_segment),
             "end": float((index + 1) * per_segment)} for index in range(count)]


def _payload(segments, frames_per_segment=10):
    return {
        "frames": [{"path": "f%03d.png" % index, "timestamp": float(index)}
                   for index in range(len(segments) * frames_per_segment)],
        "segments": segments,
    }


def test_allocate_frame_quotas_splits_whole_video_budget():
    """3 段 ×10 候选、上限 8：总配额 = 8，每段至少 1 帧。"""
    quotas = frames_mod.allocate_frame_quotas([10, 10, 10], 8)
    assert sum(quotas) == 8
    assert all(quota >= 1 for quota in quotas)


def test_allocate_frame_quotas_spreads_segments_beyond_cap():
    """段数 > 上限：按段均匀覆盖，部分段 0 帧。"""
    quotas = frames_mod.allocate_frame_quotas([10, 10, 10], 2)
    assert quotas == [1, 0, 1]
    assert sum(quotas) == 2


def test_allocate_frame_quotas_skips_segments_without_candidates():
    quotas = frames_mod.allocate_frame_quotas([5, 0, 5], 8)
    assert quotas[1] == 0
    assert quotas[0] >= 1 and quotas[2] >= 1
    assert sum(quotas) == 8


def test_allocate_frame_quotas_uses_all_frames_when_fewer_than_cap():
    assert frames_mod.allocate_frame_quotas([2, 2], 8) == [2, 2]
    assert frames_mod.allocate_frame_quotas([1, 1], 8) == [1, 1]


def test_allocate_frame_quotas_clamps_to_table1_range():
    assert sum(frames_mod.allocate_frame_quotas([10, 10], 0)) == 1
    assert sum(frames_mod.allocate_frame_quotas([10, 10], 99)) == 8


def test_frame_plan_keeps_total_within_whole_video_cap():
    segments = _segments(3)
    plan = frames_mod.frame_plan(_payload(segments), segments, 8)
    assert plan["limit"] == 8
    assert plan["frames_used"] == 8
    assert plan["frames_within_budget"] is True
    assert sum(plan["quotas"]) == 8
    assert sum(len(group) for group in plan["groups"]) == 8
    assert all(len(group) <= quota
               for group, quota in zip(plan["groups"], plan["quotas"]))


def test_frame_plan_zero_quota_segment_is_empty():
    """段数 > 上限：未覆盖的段 0 帧，其配额不占用总预算。"""
    segments = _segments(3)
    plan = frames_mod.frame_plan(_payload(segments), segments, 2)
    assert [len(group) for group in plan["groups"]] == [1, 0, 1]
    assert plan["frames_used"] == 2
    assert plan["frames_within_budget"] is True


def test_single_segment_selection_is_unchanged_by_budget_rule():
    """单语义段时与现有抽帧规则（保序均匀）完全等价。"""
    payload = {"frames": [{"path": "f%02d.png" % index, "timestamp": float(index)}
                          for index in range(20)]}
    segments = frames_mod.resolve_segments(payload, "描述", None)
    plan = frames_mod.frame_plan(payload, segments, 8)
    picked = frames_mod.select_frames(payload["frames"], None, None, 8)
    assert [item["path"] for item in plan["groups"][0]] == \
        [item["path"] for item in picked]
    assert len(plan["groups"][0]) == 8
