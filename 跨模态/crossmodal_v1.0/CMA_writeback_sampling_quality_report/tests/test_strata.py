"""分层维度解析与确定性分层抽样的单元测试（表1 #24 研发测边界）。"""
from __future__ import annotations

import math
import random

import pytest

from tests.conftest import make_record
from utils import strata as strata_mod


# --------------------------------------------------------------------------- #
# 分层维度解析
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("field,value", [
    ("scene_id", "scene-A"),
    ("scene", "scene-A"),
    ("group_id", "scene-A"),
])
def test_resolve_scene_accepts_known_fields(field, value):
    record = {"sample_id": "x", field: value}
    resolved, applicable = strata_mod.resolve_scene(record)
    assert (resolved, applicable) == (value, True)


def test_resolve_scene_falls_back_to_metadata_then_unknown():
    assert strata_mod.resolve_scene(
        {"sample_id": "x", "metadata": {"scene_id": "s-9"}}) == ("s-9", True)
    assert strata_mod.resolve_scene(
        {"sample_id": "x", "metadata": {"scene": "s-8"}}) == ("s-8", True)
    value, applicable = strata_mod.resolve_scene({"sample_id": "x"})
    assert value == strata_mod.UNKNOWN
    assert applicable is False


def test_resolve_scene_ignores_blank_values():
    value, applicable = strata_mod.resolve_scene({"sample_id": "x", "scene_id": "   "})
    assert (value, applicable) == (strata_mod.UNKNOWN, False)


@pytest.mark.parametrize("modalities,expected", [
    (("text", "image"), "image+text"),
    (("image", "text"), "image+text"),
    (("text", "audio"), "audio+text"),
    (("text", "video"), "text+video"),
    (("text", "audio", "video", "image"), "audio+image+text+video"),
])
def test_resolve_modality_builds_sorted_route(modalities, expected):
    record = make_record("m-1", modalities=modalities)
    value, applicable = strata_mod.resolve_modality(record)
    assert (value, applicable) == (expected, True)


def test_resolve_modality_without_modality_is_not_applicable():
    value, applicable = strata_mod.resolve_modality({"sample_id": "x"})
    assert (value, applicable) == (strata_mod.UNKNOWN, False)


def test_resolve_modality_accepts_string_and_list_media_fields():
    record = {"sample_id": "x", "image": ["media/a.png"], "text": "描述"}
    assert strata_mod.resolve_modality(record) == ("image+text", True)


@pytest.mark.parametrize("raw,expected", [
    ("auto_aligned", strata_mod.ALIGNMENT_AUTO),
    ("aligned", strata_mod.ALIGNMENT_AUTO),
    ("manual_aligned", strata_mod.ALIGNMENT_MANUAL),
    ("not_aligned", strata_mod.ALIGNMENT_NOT),
    ("uncertain", strata_mod.ALIGNMENT_UNCERTAIN),
    ("pending_manual", strata_mod.ALIGNMENT_PENDING),
    ("whatever", strata_mod.ALIGNMENT_OTHER),
])
def test_resolve_alignment_normalises_values(raw, expected):
    record = {"sample_id": "x", "alignment": {"status": raw}}
    value, applicable, auto = strata_mod.resolve_alignment(record)
    assert value == expected
    assert applicable is True
    assert auto is (expected == strata_mod.ALIGNMENT_AUTO)


def test_resolve_alignment_reads_processing_16_when_alignment_absent():
    record = {"sample_id": "x", "processing": {"16": {"status": "auto_aligned"}}}
    value, applicable, auto = strata_mod.resolve_alignment(record)
    assert (value, applicable, auto) == (strata_mod.ALIGNMENT_AUTO, True, True)


def test_resolve_alignment_without_evidence_is_not_applicable():
    value, applicable, auto = strata_mod.resolve_alignment({"sample_id": "x"})
    assert (value, applicable, auto) == (strata_mod.UNKNOWN, False, False)


def test_stratum_key_and_label_are_stable():
    record = make_record("k-1", scene="scene-A", modalities=("text", "image"),
                         alignment="auto_aligned")
    key, applicable = strata_mod.stratum_key(record)
    assert key == ("scene-A", "image+text", strata_mod.ALIGNMENT_AUTO)
    assert applicable == {"scene": True, "modality": True, "alignment_type": True}
    assert strata_mod.stratum_label(key) == (
        "scene=scene-A|modality=image+text|alignment_type=auto_aligned")


def test_stratum_key_reports_inapplicable_dimensions():
    record = make_record("k-2", scene=None, modalities=("text",), alignment=None)
    key, applicable = strata_mod.stratum_key(record)
    assert key[0] == strata_mod.UNKNOWN
    assert key[2] == strata_mod.UNKNOWN
    assert applicable == {"scene": False, "modality": True, "alignment_type": False}


# --------------------------------------------------------------------------- #
# 配额分配（比例 + 最大余数，并夹在 10%–20% 内）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sizes,rate,expected", [
    ([16, 12, 8, 4], 0.20, [3, 2, 2, 1]),
    ([16, 12, 8, 4], 0.10, [2, 1, 1, 0]),
    ([30, 30, 30], 0.10, [3, 3, 3]),
    ([20, 20, 20, 20], 0.15, [3, 3, 3, 3]),
    ([], 0.10, []),
    ([0, 0], 0.10, [0, 0]),
])
def test_allocate_quotas_examples(sizes, rate, expected):
    assert strata_mod.allocate_quotas(sizes, rate=rate) == expected


def test_allocate_quotas_never_exceeds_stratum_size():
    sizes = [1, 2, 3, 100]
    quotas = strata_mod.allocate_quotas(sizes, rate=0.20)
    assert all(q <= s for q, s in zip(quotas, sizes))


@pytest.mark.parametrize("sizes,rate", [([1], 0.10), ([2], 0.10), ([4], 0.20),
                                        ([1, 1], 0.15), ([3, 1], 0.20)])
def test_allocate_quotas_never_breaks_the_20_percent_cap_on_tiny_pool(sizes, rate):
    """表1 #24「不另设第二套抽检比例」：候选池过小时一条都不抽，不得越界。"""
    quotas = strata_mod.allocate_quotas(sizes, rate=rate)
    total = sum(sizes)
    assert sum(quotas) == 0
    assert sum(quotas) <= int(math.floor(0.20 * total))


def test_allocate_quotas_stays_within_percentage_window():
    rng = random.Random(20261008)
    for _ in range(200):
        sizes = [rng.randint(0, 120) for _ in range(rng.randint(1, 8))]
        total = sum(sizes)
        if total < 5:
            continue
        quotas = strata_mod.allocate_quotas(sizes, rate=0.15)
        assert sum(quotas) <= int(math.floor(0.20 * total))
        assert sum(quotas) >= int(math.ceil(0.10 * total))


# --------------------------------------------------------------------------- #
# 确定性分层抽样
# --------------------------------------------------------------------------- #

def _items(records):
    result = []
    for record in records:
        key, applicable = strata_mod.stratum_key(record)
        result.append({"sample_id": record["sample_id"], "stratum": key,
                       "applicable": applicable})
    return result


def test_select_samples_is_deterministic_and_order_independent():
    records = [make_record("d-%02d" % index,
                           modalities=("text", "image") if index % 3 else ("text", "audio"))
               for index in range(30)]
    items = _items(records)
    first = strata_mod.select_samples(items, rate=0.20, salt="operator24")
    shuffled = list(items)
    random.Random(7).shuffle(shuffled)
    second = strata_mod.select_samples(shuffled, rate=0.20, salt="operator24")
    assert set(first["selected"]) == set(second["selected"])


def test_select_samples_changes_with_salt():
    records = [make_record("s-%03d" % index) for index in range(60)]
    items = _items(records)
    first = strata_mod.select_samples(items, rate=0.10, salt="operator24")
    second = strata_mod.select_samples(items, rate=0.10, salt="other-salt")
    assert set(first["selected"]) != set(second["selected"])


def test_select_samples_keeps_global_rate_inside_window():
    records = [make_record("r-%03d" % index,
                           scene="scene-%d" % (index % 4),
                           modalities=(("text", "image") if index % 2
                                       else ("text", "video")))
               for index in range(120)]
    result = strata_mod.select_samples(_items(records), rate=0.10, salt="operator24")
    assert len(result["selected"]) == result["target_total"]
    assert 0.10 - 1e-9 <= result["achieved_rate"] <= 0.20 + 1e-9


def test_select_samples_reports_per_stratum_breakdown():
    records = ([make_record("a-%02d" % index) for index in range(20)]
               + [make_record("b-%02d" % index, modalities=("text", "audio"))
                  for index in range(12)]
               + [make_record("c-%02d" % index, scene="scene-sd",
                              modalities=("text", "video")) for index in range(8)])
    result = strata_mod.select_samples(_items(records), rate=0.20, salt="operator24")
    assert result["target_total"] == 8
    sizes = sorted(item["size"] for item in result["strata"].values())
    quotas = sorted(item["target"] for item in result["strata"].values())
    assert sizes == [8, 12, 20]
    assert sum(quotas) == 8
    for item in result["strata"].values():
        assert item["sampled"] == len(item["sample_ids"])


def test_select_samples_handles_empty_input():
    result = strata_mod.select_samples([], rate=0.10, salt="operator24")
    assert result["selected"] == []
    assert result["target_total"] == 0
    assert result["achieved_rate"] == 0.0
    assert result["strata"] == {}


def test_select_samples_rejects_out_of_domain_rate():
    for bad in (0.05, 0.25, -0.1):
        with pytest.raises(ValueError):
            strata_mod.select_samples([], rate=bad, salt="operator24")
