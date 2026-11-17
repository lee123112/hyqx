"""业务处理、配置、路径安全与统计守恒测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import inference
from tests import fixtures
from utils import audio_text
from utils import io as io_utils
from utils import schemas
from utils import scoring as scoring_mod


#: 100 个互不相同的中文字符（千字文），用于构造真实感文本：
#: 重复同一字符会被「转写可靠性门」判为解码循环，测试文本必须避免这种情况。
_DISTINCT_CHARS = (
    "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨"
    "露结为霜金生丽水玉出昆冈剑号巨阙珠称夜光果珍李柰菜重芥姜海咸河淡鳞潜羽翔"
    "龙师火帝鸟官人皇始制文字乃服衣裳推位让国有虞陶唐吊民伐罪周发殷汤")

#: 与上表完全不重叠的第二组互异字符（千字文续，剔除重复字），用于「逐位替换」
#: 构造精确 CER：替换字符与原字符不同，编辑距离才正好等于被替换的位数。
_DISTINCT_CHARS_ALT = "".join(
    char for char in dict.fromkeys(
        "坐朝问道垂拱平章爱育黎首臣伏戎羌遐迩一体率宾归王鸣凤在竹白驹食场化被草木"
        "赖及万方盖此身发四大五常恭惟鞠养岂敢毁伤女慕贞洁男效才良知过必改得能莫忘"
        "罔谈彼短靡恃己长信使可覆器欲难量墨悲丝染诗赞羔羊景行维贤克念作圣德建名立"
        "形端表正空谷传声虚堂习听祸因恶积福缘善庆尺璧非宝寸阴是竞")
    if char not in set(_DISTINCT_CHARS))
assert len(_DISTINCT_CHARS_ALT) >= 100, len(_DISTINCT_CHARS_ALT)

# --------------------------------------------------------------------------- #
# 字段解析
# --------------------------------------------------------------------------- #

def test_resolve_audio_reference_uses_first_segment(tmp_path):
    record = {
        "sample_id": "s1",
        "audio": {
            "path": "media/whole.wav",
            "duration": 9.0,
            "segments": [{"path": "media/seg_a.wav", "start": 1.0, "end": 2.5}],
        },
    }
    resolved = audio_text.resolve_audio_reference(record)
    assert resolved["path"] == "media/seg_a.wav"
    assert resolved["start"] == pytest.approx(1.0)
    assert resolved["end"] == pytest.approx(2.5)
    assert resolved["source"] == "audio.segments[0]"


def test_resolve_audio_reference_falls_back_to_audio_path():
    record = {"sample_id": "s1", "audio": {"path": "media/whole.wav", "duration": 3.0}}
    resolved = audio_text.resolve_audio_reference(record)
    assert resolved["path"] == "media/whole.wav"
    assert resolved["start"] == 0.0
    assert resolved["end"] == pytest.approx(3.0)


def test_resolve_audio_reference_accepts_plain_string():
    resolved = audio_text.resolve_audio_reference({"sample_id": "s1", "audio": "media/a.wav"})
    assert resolved["path"] == "media/a.wav"


def test_resolve_audio_reference_returns_none_without_audio():
    assert audio_text.resolve_audio_reference({"sample_id": "s1", "audio": None}) is None


@pytest.mark.parametrize("record,expected,source", [
    ({"audio": {"path": "a.wav", "transcript": "音频转写"}}, "音频转写", "audio.transcript"),
    ({"audio": {"path": "a.wav", "segments": [{"path": "s.wav", "transcript": "段落转写"}]}},
     "段落转写", "audio.segments[0].transcript"),
    ({"text": {"transcript": "文本侧转写"}}, "文本侧转写", "text.transcript"),
    ({"processing": {"08": {"transcript": "节点转写"}}}, "节点转写", "processing.08.transcript"),
    ({"metadata": {"transcript": "元数据转写"}}, "元数据转写", "metadata.transcript"),
])
def test_resolve_transcript_sources(record, expected, source):
    resolved = audio_text.resolve_transcript(record)
    assert resolved["value"] == expected
    assert resolved["source"] == source


def test_resolve_transcript_returns_none_when_absent():
    assert audio_text.resolve_transcript({"audio": {"path": "a.wav"}})["value"] is None


@pytest.mark.parametrize("record,expected,source", [
    ({"text": {"content": "文本描述"}}, "文本描述", "text.content"),
    ({"metadata": {"description": "元数据描述"}}, "元数据描述", "metadata.description"),
    ({"processing": {"09": {"description": "节点描述"}}}, "节点描述", "processing.09.description"),
    ({"text": {"original": "原始描述"}}, "原始描述", "text.original"),
])
def test_resolve_description_sources(record, expected, source):
    resolved = audio_text.resolve_description(record)
    assert resolved["value"] == expected
    assert resolved["source"] == source


# --------------------------------------------------------------------------- #
# 关闭路线（表1 默认态）
# --------------------------------------------------------------------------- #

def test_disabled_route_passthrough(tmp_path):
    media = fixtures.default_media()
    records = [
        fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好"),
        fixtures.make_flat_record("flat-1", transcript="我们一起去公园散步",
                                  description="我们一起去公园散步", audio_path="media/seg_02.wav"),
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_disabled", output_root=tmp_path / "out",
                           allow_external_input=True)
    assert result["status"] == "success"
    assert result["entrypoint"] == audio_text.MAIN_NAME_DISABLED
    stats = result["statistics"]
    assert stats["enabled"] is False
    assert stats["audio_text_file_written"] is False
    assert stats["skipped_records"] == 2
    assert stats["scored_records"] == 0
    assert stats["models_available"] is False
    assert stats["conservation_ok"] is True
    rows = fixtures.read_jsonl(result["outputFile"])
    assert len(rows) == 2
    for row in rows:
        # [v4.2] status 双字段：int status + 原字符串迁入 process_status
        assert row["processing"]["13"]["status"] == 1
        assert row["processing"]["13"]["process_status"] == "skipped"
        assert row["processing"]["13"]["route_enabled"] is False
        assert "cosine_similarity" not in row["processing"]["13"]
    # 未写音文评分文件、未隔离
    assert not (Path(result["runDir"]) / audio_text.MAIN_NAME_ENABLED).exists()
    assert fixtures.read_jsonl(Path(result["runDir"]) / audio_text.SAMPLE_QUARANTINE_NAME) == []


def test_disabled_route_preserves_upstream_fields(tmp_path):
    record = fixtures.make_pair("pair-keep", transcript="今天天气很好", description="今天天气很好")
    record.setdefault("processing", {})["11"] = {"status": "success"}
    record["custom_extension"] = {"keep": True}
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_keep", output_root=tmp_path / "out",
                           allow_external_input=True)
    row = fixtures.read_jsonl(result["outputFile"])[0]
    assert row["processing"]["11"] == {"status": "success"}
    assert row["custom_extension"] == {"keep": True}
    assert row["pair_id"] == "pair-keep"
    assert row["left"]["record"]["audio"]["transcript"] == "今天天气很好"


# --------------------------------------------------------------------------- #
# 模型依赖：显式失败 / 显式降级（绝不伪造分数）
# --------------------------------------------------------------------------- #

@pytest.mark.skip(reason="v4.2 §4.13：M13 改为纯文本 CER/编辑距离规则，不再加载模型，无模型依赖失败语义")
def test_enabled_without_models_and_without_degraded_fails(tmp_path, strict_config_path):
    entry = fixtures.build_input_tree(
        tmp_path / "in",
        [fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好")],
        media=fixtures.default_media())
    with pytest.raises(inference.DependencyError):
        inference.run(str(entry / "demo.jsonl"), "task_strict", config_path=str(strict_config_path),
                      output_root=tmp_path / "out", allow_external_input=True)


@pytest.mark.skip(reason="v4.2 §4.13：不再有降级模式；开启路线即按 CER 规则评分")
def test_enabled_degraded_mode_produces_no_fake_scores(tmp_path, degraded_config_path):
    media = fixtures.default_media()
    records = [
        fixtures.make_pair("pair-pending", transcript="今天天气很好", description="今天天气很好"),
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_degraded",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    assert result["status"] == "degraded"
    stats = result["statistics"]
    assert stats["degraded"] is True
    assert stats["degraded_records"] == 1
    assert stats["scored_records"] == 0
    assert stats["passed_records"] == 0
    assert stats["sample_isolated"] == 0
    assert stats["models_available"] is False
    assert stats["audio_text_file_written"] is True
    # 通过结果才交 #19：降级下没有任何记录通过，主产物不得含任何分数
    assert fixtures.read_jsonl(result["outputFile"]) == []
    pending = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.PENDING_NAME)
    assert len(pending) == 1
    assert pending[0]["label"] == audio_text.LABEL_FLOW_PENDING_SCORE
    assert pending[0]["destination"] == audio_text.DEST_PIPELINE
    node = pending[0]["audio_text"]
    assert node["status"] == "degraded"
    assert node["passed"] is None
    assert node["cosine_similarity"] is None
    assert node["audio_model"] is None and node["text_model"] is None
    assert node["edit_distance_ratio"] == pytest.approx(0.0)
    assert pending[0]["record"]["processing"]["13"] == node
    assert fixtures.read_jsonl(Path(result["runDir"]) / audio_text.SAMPLE_QUARANTINE_NAME) == []


def test_edit_distance_rejection_works_without_models(tmp_path, degraded_config_path):
    """通过条件必须同时满足两项；编辑距离超标即可判不通过，无需模型。"""
    media = fixtures.default_media()
    records = [
        fixtures.make_pair("pair-far", transcript="今天天气很好", description="完全不同的一段无关内容"),
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_edit",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    stats = result["statistics"]
    assert stats["edit_distance_exceeded"] == 1
    assert stats["sample_isolated"] == 1
    assert stats["degraded_records"] == 0
    # 不通过样本不得出现在主产物（通过结果才交 #19），只进样本隔离台账
    assert fixtures.read_jsonl(result["outputFile"]) == []
    quarantined = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.SAMPLE_QUARANTINE_NAME)
    assert len(quarantined) == 1
    node = quarantined[0]["audio_text"]
    assert node["passed"] is False
    assert node["reason"] == "edit_distance_exceeded"
    assert node["cosine_similarity"] is None
    assert node["edit_distance_ratio"] > 0.30
    assert node["destination"] == audio_text.DEST_SAMPLE
    assert quarantined[0]["label"] == audio_text.LABEL_SAMPLE_SEMANTIC
    assert quarantined[0]["destination"] == audio_text.DEST_SAMPLE
    assert quarantined[0]["record"]["processing"]["13"]["passed"] is False


# --------------------------------------------------------------------------- #
# 真实模型路径（微型本地权重，仅验证契约与路由）
# --------------------------------------------------------------------------- #

def test_enabled_route_rule_only_scores_without_models(tmp_path, tiny_config_path, tiny_models):
    """[v4.2] 生产判定 = 上游转写文本的 CER/编辑距离比例；不加载任何模型。"""
    media = fixtures.default_media()
    records = [
        fixtures.make_pair("pair-a", transcript="音频文本一致性测试样例", description="音频文本一致性测试样例"),
        fixtures.make_pair("pair-b", transcript="今天天气很好", description="今天天气很好",
                           audio_path="media/seg_02.wav"),
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_tiny", config_path=str(tiny_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    assert result["status"] == "success"
    stats = result["statistics"]
    # 规则路线完整可用，不属于依赖降级
    assert stats["models_available"] is False
    assert stats["scored_records"] == 2
    assert stats["passed_records"] == 2
    assert stats["edit_distance_exceeded"] == 0
    assert stats["degraded"] is False
    assert stats["conservation_ok"] is True
    rows = fixtures.read_jsonl(result["outputFile"])
    assert len(rows) == 2
    for row in rows:
        node = row["processing"]["13"]
        assert node["status"] == 1
        assert node["process_status"] == "passed"
        assert node["passed"] is True
        assert node["edit_distance_ratio"] == pytest.approx(0.0)
        assert node["scoring_method"] == "character_edit_distance_ratio"


@pytest.mark.parametrize("edit_ratio,expect_pass", [
    (0.0, True), (0.25, True), (0.30, True), (0.31, False), (1.0, False),
])
def test_decision_boundary_uses_edit_distance_threshold(tmp_path, degraded_config_path,
                                                        edit_ratio, expect_pass):
    """[v4.2 §4.13] 判定只由 CER/编辑距离比例决定；裸余弦不参与（替身仅作诊断）。

    100 个互不相同的字符：既避开「解码重复循环」判定，也让 100 字落在 10 秒音频里
    （10 字/秒）不触发转写可靠性门的语速上限。
    """
    text_length = 100
    chars = int(round(edit_ratio * text_length))
    alphabet = _DISTINCT_CHARS[:text_length]
    description = alphabet
    transcript = alphabet[:text_length - chars] + _DISTINCT_CHARS_ALT[:chars]
    media = {"media/seg_01.wav": fixtures.wav_bytes(duration=10.0, freq=180.0)}
    records = [fixtures.make_pair("pair-edge", transcript=transcript, description=description)]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    stub = fixtures.StubScorer(cosine=0.01)
    result = inference.run(str(entry / "demo.jsonl"), "task_edge",
                           config_path=str(degraded_config_path), output_root=tmp_path / "out",
                           allow_external_input=True, scorer=stub)
    verdict, why = scoring_mod.decide_edit(
        inference_metrics_ratio(result, "pair-edge"), edit_ratio_max=0.30)
    assert verdict is expect_pass
    rows = fixtures.read_jsonl(result["outputFile"])
    if expect_pass:
        assert len(rows) == 1
        node = rows[0]["processing"]["13"]
        assert node["passed"] is True
        assert node["status"] == 1 and node["process_status"] == "passed"
        assert node["destination"] == audio_text.DEST_NEXT
        # 裸余弦只在诊断字段，不改变结论
        assert node["diag_cosine"] == pytest.approx(0.01)
    else:
        assert rows == []
        quarantined = fixtures.read_jsonl(
            Path(result["runDir"]) / audio_text.SAMPLE_QUARANTINE_NAME)
        node = quarantined[0]["audio_text"]
        assert node["passed"] is False
        assert node["status"] == 0 and node["process_status"] == "failed"
        assert node["reason"] == "edit_distance_exceeded"
    assert stub.calls == 1


def inference_metrics_ratio(result, pair_id):
    """从隔离/主产物中取该 pair 的真实编辑距离比例（供判定复核）。"""
    for name in (audio_text.SAMPLE_QUARANTINE_NAME, result["entrypoint"]):
        path = Path(result["runDir"]) / name
        if not path.is_file():
            continue
        for row in fixtures.read_jsonl(path):
            node = row.get("audio_text") or (row.get("processing") or {}).get("13")
            if node and (row.get("pair_id") == pair_id or
                         (row.get("record") or {}).get("pair_id") == pair_id):
                return node["edit_distance_ratio"]
    raise AssertionError("找不到 pair 的评分节点：%s" % pair_id)


def test_edit_ratio_exactly_at_threshold_passes_when_cosine_ok(tmp_path, degraded_config_path):
    media = fixtures.default_media()
    # "甲乙丙丁" vs "甲乙丙戊" → 编辑距离比例 0.25 < 0.30
    # （用中文字符：本算子 CER 是中文口径，纯 ASCII 描述会被语言适用性门判 not_applicable）
    records = [fixtures.make_pair("pair-ratio", transcript="甲乙丙丁", description="甲乙丙戊")]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_ratio",
                           config_path=str(degraded_config_path), output_root=tmp_path / "out",
                           allow_external_input=True, scorer=fixtures.StubScorer(cosine=0.30))
    node = fixtures.read_jsonl(result["outputFile"])[0]["processing"]["13"]
    assert node["edit_distance_ratio"] == pytest.approx(0.25)
    assert node["passed"] is True


# --------------------------------------------------------------------------- #
# 流程状态（不隔离）与原始文件问题（隔离）
# --------------------------------------------------------------------------- #

def test_missing_transcript_goes_to_pending_not_quarantine(tmp_path, degraded_config_path):
    record = fixtures.make_pair("pair-notrans", transcript="", description="今天天气很好")
    record["left"]["record"]["audio"].pop("transcript", None)
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_notrans",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    stats = result["statistics"]
    assert stats["missing_transcript"] == 1
    assert stats["pending_records"] == 1
    assert stats["sample_isolated"] == 0
    pending = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.PENDING_NAME)
    assert pending[0]["label"] == audio_text.LABEL_FLOW_PENDING_TRANSCRIPT
    assert pending[0]["destination"] == audio_text.DEST_PIPELINE
    node = pending[0]["audio_text"]
    assert node["passed"] is None and node["cosine_similarity"] is None


def test_missing_description_goes_to_pending(tmp_path, degraded_config_path):
    record = fixtures.make_pair("pair-nodesc", transcript="今天天气很好", description="今天天气很好")
    record["right"]["record"]["text"] = None
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_nodesc",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    stats = result["statistics"]
    assert stats["missing_description"] == 1
    pending = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.PENDING_NAME)
    assert pending[0]["label"] == audio_text.LABEL_FLOW_PENDING_DESCRIPTION
    assert stats["sample_isolated"] == 0


def test_pair_without_audio_is_flow_status(tmp_path, degraded_config_path):
    record = fixtures.make_pair("pair-noaudio", transcript="今天天气很好", description="今天天气很好")
    for side in ("left", "right"):
        record[side]["record"]["audio"] = None
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_noaudio",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    stats = result["statistics"]
    assert stats["no_audio_text_pair"] == 1
    assert stats["sample_isolated"] == 0
    pending = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.PENDING_NAME)
    assert pending[0]["label"] == audio_text.LABEL_FLOW_NO_AUDIO_TEXT


def test_missing_audio_file_is_original_quarantine(tmp_path, degraded_config_path):
    record = fixtures.make_pair("pair-gone", transcript="今天天气很好", description="今天天气很好",
                                audio_path="media/absent.wav")
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_gone",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    stats = result["statistics"]
    assert stats["origin_quarantined"] == 1
    rejects = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.REJECTS_NAME)
    assert rejects[0]["reason"] == "audio_missing"
    assert rejects[0]["label"] == audio_text.LABEL_ORIGIN_UNPARSABLE
    assert rejects[0]["destination"] == audio_text.DEST_ORIGIN
    assert fixtures.read_jsonl(result["outputFile"]) == []


def test_silent_audio_is_original_quarantine_empty(tmp_path, degraded_config_path):
    record = fixtures.make_pair("pair-silent", transcript="今天天气很好", description="今天天气很好",
                                audio_path="media/seg_silent.wav")
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_silent",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    rejects = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.REJECTS_NAME)
    assert rejects[0]["label"] == audio_text.LABEL_ORIGIN_EMPTY
    assert result["statistics"]["origin_quarantined"] == 1


@pytest.mark.parametrize("bad_path", [
    "C:/abs/seg.wav",
    "/root/seg.wav",
    "../outside/seg.wav",
    "http://example.com/seg.wav",
    "media\\windows\\seg.wav",
])
def test_malformed_audio_reference_is_original_quarantine(tmp_path, degraded_config_path, bad_path):
    record = fixtures.make_pair("pair-badpath", transcript="今天天气很好", description="今天天气很好",
                                audio_path=bad_path)
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_badpath",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    rejects = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.REJECTS_NAME)
    assert rejects and rejects[0]["label"] == audio_text.LABEL_ORIGIN_UNPARSABLE
    assert result["statistics"]["origin_quarantined"] == 1


# --------------------------------------------------------------------------- #
# 坏行 / 缺字段 / 编码
# --------------------------------------------------------------------------- #

def test_corrupted_json_line_goes_to_rejects(tmp_path):
    raw_lines = [
        json.dumps(fixtures.make_pair("pair-1", transcript="今天天气很好",
                                      description="今天天气很好"), ensure_ascii=False),
        "{not json at all",
        "[1, 2, 3]",
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", None, media=fixtures.default_media(),
                                      raw_lines=raw_lines)
    result = inference.run(str(entry / "demo.jsonl"), "task_bad_json", output_root=tmp_path / "out",
                           allow_external_input=True)
    stats = result["statistics"]
    assert stats["input_records"] == 3
    assert stats["bad_lines"] == 2
    assert stats["valid_records"] == 1
    assert stats["conservation_ok"] is True
    reasons = [row["reason"] for row in fixtures.read_jsonl(Path(result["runDir"]) / audio_text.REJECTS_NAME)]
    assert "json_parse_error" in reasons
    assert "record_not_object" in reasons


def test_missing_identifier_is_original_quarantine(tmp_path):
    record = fixtures.make_pair("pair-x", transcript="今天天气很好", description="今天天气很好")
    record.pop("pair_id")
    for side in ("left", "right"):
        record[side].pop("sample_id", None)
        record[side]["record"].pop("sample_id", None)
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_noid", output_root=tmp_path / "out",
                           allow_external_input=True)
    rejects = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.REJECTS_NAME)
    assert rejects[0]["reason"] == "missing_field: sample_id"
    assert rejects[0]["label"] == audio_text.LABEL_ORIGIN_MISSING_FIELD
    assert result["statistics"]["origin_quarantined"] == 1


def test_empty_input_file_is_success(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    result = inference.run(str(path), "task_empty", output_root=tmp_path / "out",
                           allow_external_input=True)
    assert result["status"] == "success"
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["input_records"] == 0
    assert result["statistics"]["conservation_ok"] is True
    assert fixtures.read_jsonl(result["outputFile"]) == []


def test_blank_lines_are_ignored_but_counted_separately(tmp_path):
    raw_lines = [
        json.dumps(fixtures.make_pair("pair-1", transcript="今天天气很好",
                                      description="今天天气很好"), ensure_ascii=False),
        "",
        "   ",
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", None, media=fixtures.default_media(),
                                      raw_lines=raw_lines)
    result = inference.run(str(entry / "demo.jsonl"), "task_blank", output_root=tmp_path / "out",
                           allow_external_input=True)
    stats = result["statistics"]
    assert stats["input_records"] == 1
    assert stats["blank_lines"] == 2
    assert stats["conservation_ok"] is True


def test_wrong_encoding_reports_encoding_error(tmp_path):
    record = fixtures.make_pair("pair-gbk", transcript="今天天气很好", description="今天天气很好")
    entry = fixtures.build_input_tree(tmp_path / "in", [record], media=fixtures.default_media(),
                                      encoding="gbk")
    result = inference.run(str(entry / "demo.jsonl"), "task_gbk", output_root=tmp_path / "out",
                           allow_external_input=True)
    assert result["status"] == "failed"
    rejects = fixtures.read_jsonl(Path(result["runDir"]) / audio_text.REJECTS_NAME)
    assert rejects[0]["reason"] == "encoding_error"
    assert rejects[0]["label"] == audio_text.LABEL_ORIGIN_ENCODING
    assert result["statistics"]["conservation_ok"] is True


def test_missing_input_file_raises(tmp_path):
    with pytest.raises(inference.InputError):
        inference.run(str(tmp_path / "absent.jsonl"), "task_absent", output_root=tmp_path / "out",
                      allow_external_input=True)


# --------------------------------------------------------------------------- #
# 任务隔离 / 重复运行 / 路径安全
# --------------------------------------------------------------------------- #

def test_two_unique_ids_are_isolated(tmp_path):
    media = fixtures.default_media()
    first = fixtures.build_input_tree(tmp_path / "in1",
                                      [fixtures.make_pair("pair-1", transcript="今天天气很好",
                                                          description="今天天气很好")], media=media)
    second = fixtures.build_input_tree(tmp_path / "in2",
                                       [fixtures.make_pair("pair-2", transcript="我们一起去公园散步",
                                                           description="我们一起去公园散步")],
                                       media=media)
    root = tmp_path / "out"
    r1 = inference.run(str(first / "demo.jsonl"), "task_one", output_root=root,
                       allow_external_input=True)
    r2 = inference.run(str(second / "demo.jsonl"), "task_two", output_root=root,
                       allow_external_input=True)
    assert r1["runDir"] != r2["runDir"]
    assert "task_one" in r1["runDir"] and "task_two" in r2["runDir"]
    row1 = fixtures.read_jsonl(r1["outputFile"])[0]
    row2 = fixtures.read_jsonl(r2["outputFile"])[0]
    assert row1["pair_id"] == "pair-1"
    assert row2["pair_id"] == "pair-2"


def test_repeated_runs_do_not_overwrite(tmp_path):
    entry = fixtures.build_input_tree(
        tmp_path / "in",
        [fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好")],
        media=fixtures.default_media())
    root = tmp_path / "out"
    first = inference.run(str(entry / "demo.jsonl"), "task_repeat", output_root=root,
                          allow_external_input=True)
    second = inference.run(str(entry / "demo.jsonl"), "task_repeat", output_root=root,
                           allow_external_input=True)
    assert first["runId"] != second["runId"]
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["outputFile"]).is_file() and Path(second["outputFile"]).is_file()


@pytest.mark.parametrize("bad", ["", "  ", ".", "..", "a/b", "a\\b", "C:", "con", "trail."])
def test_invalid_unique_id_rejected(bad):
    with pytest.raises(ValueError):
        io_utils.validate_unique_id(bad)


def test_input_file_outside_task_is_rejected(tmp_path, output_root):
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    import os

    os.environ["OUTPUT_DIR"] = str(output_root)
    task = io_utils.task_dir(output_root, "task_scope")
    with pytest.raises(ValueError):
        io_utils.resolve_in_task(task, str(outside))


def test_input_inside_task_is_accepted(tmp_path, output_root):
    task = io_utils.task_dir(output_root, "task_scope")
    inside = task / "input" / "demo.jsonl"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("", encoding="utf-8")
    resolved = io_utils.resolve_in_task(task, str(inside))
    assert resolved == inside.resolve()
    assert io_utils.resolve_in_task(task, "input/demo.jsonl") == inside.resolve()


# --------------------------------------------------------------------------- #
# 配置校验
# --------------------------------------------------------------------------- #

def test_config_defaults_disabled():
    config = inference.load_config()
    assert config.enabled is False
    assert config.edit_distance_ratio_max == pytest.approx(0.30)
    # [v4.2 §4.13] 裸余弦阈值不再出现在 config；judge_mode 默认 operator
    assert config.judge_mode == schemas.DEFAULT_JUDGE_MODE
    assert not hasattr(config, "cosine_threshold")
    assert config.stage_id == "S3"


@pytest.mark.parametrize("overrides", [
    {"edit_distance_ratio_max": 0.05},
    {"edit_distance_ratio_max": 0.51},
    {"cosine_threshold": 0.19},          # v4.2：cosine_threshold 已从 config 移除，严格拒绝
    {"judge_mode": "other"},
    {"sample_rate": 8000},
    {"max_text_chars": 0},
    {"max_audio_seconds": 0},
    {"enabled": "yes"},
    {"allow_degraded": 1},
])
def test_invalid_config_values_rejected(overrides):
    with pytest.raises(schemas.ConfigError):
        inference.load_config(overrides=overrides)


def test_config_boundary_values_accepted():
    config = inference.load_config(overrides={"edit_distance_ratio_max": 0.10})
    assert config.edit_distance_ratio_max == pytest.approx(0.10)
    config = inference.load_config(overrides={"edit_distance_ratio_max": 0.50,
                                              "judge_mode": "system"})
    assert config.edit_distance_ratio_max == pytest.approx(0.50)
    assert config.judge_mode == "system"


def test_readonly_and_unknown_keys_rejected():
    for key in ("audio_model", "text_model", "stage_id"):
        with pytest.raises(schemas.ConfigError):
            inference.load_config(overrides={key: "other"})
    with pytest.raises(schemas.ConfigError):
        inference.load_config(overrides={"not_a_key": 1})


def test_bad_config_file_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(schemas.ConfigError):
        inference.load_config(config_path=str(path))


# --------------------------------------------------------------------------- #
# 血缘 / 统计守恒 / 阶段末对象
# --------------------------------------------------------------------------- #

@pytest.mark.skip(reason="v4.2 血缘输出已停用：inference 不再产出血缘文件")
def test_lineage_fields_and_statistics_are_real(tmp_path, degraded_config_path):
    media = fixtures.default_media()
    records = [
        fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好"),
        fixtures.make_pair("pair-2", transcript="完全不同的一段无关内容", description="今天天气很好",
                           audio_path="media/seg_02.wav"),
    ]
    entry = fixtures.build_input_tree(tmp_path / "in", records, media=media)
    result = inference.run(str(entry / "demo.jsonl"), "task_lineage",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    lineage = json.loads(Path(result["lineageFile"]).read_text(encoding="utf-8"))
    record = lineage[0]
    for field in ("operator", "operator_version", "uniqueId", "inputFile", "status",
                  "startedAt", "finishedAt", "config", "statistics"):
        assert field in record, field
    assert record["operator"] == audio_text.OPERATOR
    assert record["uniqueId"] == "task_lineage"
    assert record["stageId"] == "S3"
    assert record["formalOperatorNumber"] == 13
    assert record["outputFile"] in result["outputFile"] or record["outputFile"] == result["outputFile"]
    assert record["statistics"]["input_records"] == 2
    assert record["config"]["business"]["edit_distance_ratio_max"] == pytest.approx(0.30)
    assert record["config"]["deployment"]["stage_id"] == "S3"
    assert record["config"]["overridden_keys"] == []
    assert result["runId"] == "run001"
    assert result["statistics"]["conservation_ok"] is True


@pytest.mark.skip(reason="v4.2 血缘输出已停用：build_intermediate_lineage/build_stage_payload 已注释存档")
def test_intermediate_and_stage_payload_shapes(tmp_path):
    entry = fixtures.build_input_tree(
        tmp_path / "in",
        [fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好")],
        media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_shapes", output_root=tmp_path / "out",
                           allow_external_input=True)
    intermediate = inference.build_intermediate_lineage(result)
    assert set(intermediate.keys()) >= {"operator", "uniqueId", "outputFile", "statistics"}
    assert intermediate["outputFile"] == result["outputFile"]
    stage_payload = inference.build_stage_payload(result)
    assert isinstance(stage_payload, list) and stage_payload
    assert all("outputFile" not in item for item in stage_payload)
    file_object = inference.stage_file_object(result)
    assert file_object["format"] == "crossmodal-files-v1"
    assert file_object["entrypoint"] == audio_text.MAIN_NAME_DISABLED
    paths = [item["path"] for item in file_object["files"]]
    assert audio_text.MAIN_NAME_DISABLED in paths
    assert audio_text.REJECTS_NAME in paths
    assert audio_text.PENDING_NAME in paths
    assert audio_text.SAMPLE_QUARANTINE_NAME in paths


def test_media_is_carried_into_run_directory(tmp_path, degraded_config_path):
    entry = fixtures.build_input_tree(
        tmp_path / "in",
        [fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好")],
        media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_media",
                           config_path=str(degraded_config_path),
                           output_root=tmp_path / "out", allow_external_input=True)
    assert result["statistics"]["media_carried"] == 1
    assert (Path(result["runDir"]) / "media" / "seg_01.wav").is_file()


@pytest.mark.skip(reason="v4.2 血缘输出已停用：config 快照随血缘停用")
def test_config_snapshot_records_overrides(tmp_path):
    result = inference.run(
        str(fixtures.build_input_tree(
            tmp_path / "in",
            [fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好")],
            media=fixtures.default_media()) / "demo.jsonl"),
        "task_override", output_root=tmp_path / "out", allow_external_input=True,
        overrides={"enabled": False, "edit_distance_ratio_max": 0.45})
    lineage = json.loads(Path(result["lineageFile"]).read_text(encoding="utf-8"))
    snapshot = lineage[0]["config"]
    assert snapshot["business"]["edit_distance_ratio_max"] == pytest.approx(0.45)
    assert snapshot["overridden_keys"] == ["edit_distance_ratio_max", "enabled"]
    assert snapshot["overrides"]["enabled"] is False


@pytest.mark.skip(reason="v4.2 血缘/阶段归档输出已停用：不再写 stage_context.json")
def test_stage_context_written(tmp_path):
    entry = fixtures.build_input_tree(
        tmp_path / "in",
        [fixtures.make_pair("pair-1", transcript="今天天气很好", description="今天天气很好")],
        media=fixtures.default_media())
    result = inference.run(str(entry / "demo.jsonl"), "task_ctx", output_root=tmp_path / "out",
                           allow_external_input=True)
    context = json.loads((Path(result["runDir"]) / "stage_context.json").read_text(encoding="utf-8"))
    assert context["stageId"] == "S3"
    assert context["uniqueId"] == "task_ctx"
    assert context["entrypoint"] == audio_text.MAIN_NAME_DISABLED
    assert isinstance(context["stage_lineage"], list)
