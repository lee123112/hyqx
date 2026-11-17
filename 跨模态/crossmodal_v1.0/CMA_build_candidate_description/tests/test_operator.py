"""算子 #09 准备候选描述：业务规则、边界、异常、任务隔离与统计守恒测试。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import inference  # noqa: E402

SAMPLE = BASE / "dataset" / "sample" / "demo.jsonl"
FROZEN = ["caption", "alt", "title", "subtitle", "transcript", "manual"]


@pytest.fixture(scope="session", autouse=True)
def ensure_sample():
    """样例不存在时真实生成，避免依赖未上传的媒体。"""
    if not SAMPLE.exists():
        subprocess.run([sys.executable, str(BASE / "dataset" / "sample" / "make_sample.py")],
                       check=True)


def _cfg(**overrides):
    cfg = inference.load_config()
    cfg = json.loads(json.dumps(cfg))
    cfg["description"].update(overrides)
    return cfg


# --------------------------------------------------------------------------
# 1. 来源优先级（表1冻结：图注→Alt→标题→字幕→语音转写→人工补写）
# --------------------------------------------------------------------------

def test_priority_caption_wins_over_others():
    rec = {"sample_id": "s1", "caption": "一只金毛犬在草地上接飞盘",
           "alt": "Alt 文本内容示例", "title": "标题文本内容示例",
           "subtitle": "字幕文本内容示例", "transcript": "转写文本内容示例",
           "manual_description": "人工补写内容示例"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "described"
    assert d["description_source"] == "caption"
    assert d["description"] == "一只金毛犬在草地上接飞盘"


def test_priority_alt_when_no_caption():
    rec = {"sample_id": "s1", "caption": "  ",
           "alt": "一只橘猫趴在沙发上睡觉", "title": "标题文本内容示例"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["description_source"] == "alt"


def test_priority_title_when_no_caption_alt():
    rec = {"sample_id": "s1", "title": "海边日落风景照片"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["description_source"] == "title"


def test_priority_subtitle_joins_segments():
    rec = {"sample_id": "s1",
           "subtitle": [{"text": "欢迎", "start": 0.0, "end": 1.0},
                        {"text": "观看本期视频内容", "start": 1.0, "end": 3.0}]}
    d = inference.extract_candidate(rec, _cfg())
    assert d["description_source"] == "subtitle"
    assert d["description"] == "欢迎观看本期视频内容"


def test_priority_transcript_object():
    rec = {"sample_id": "s1", "audio": {"path": "a.wav",
           "transcript": "今天讲解跨模态数据对齐流程"}}
    d = inference.extract_candidate(rec, _cfg())
    assert d["description_source"] == "transcript"
    assert "跨模态" in d["description"]


def test_priority_manual_is_last():
    rec = {"sample_id": "s1", "title": " ",
           "manual_description": "人工补写的描述内容示例"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["description_source"] == "manual"


def test_transcription_payload_ignored_by_default():
    """round9 复核场景：仅 #08 transcription.text 有内容时，默认仍待人工补写。"""
    rec = {"sample_id": "s1", "audio": {"path": "a.wav"},
           "transcription": {"status": "ok", "text": "丈夫死了的案情通报"}}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "pending_manual"


def test_transcription_payload_used_when_enabled():
    """显式开启后，#08 转写文本作为「语音转写」来源进入描述。"""
    rec = {"sample_id": "s1", "audio": {"path": "a.wav"},
           "transcription": {"status": "ok", "text": "丈夫死了的案情通报"}}
    d = inference.extract_candidate(rec, _cfg(include_transcription_payload=True))
    assert d["status"] == "described"
    assert d["description_source"] == "transcript"
    assert d["description"] == "丈夫死了的案情通报"


def test_transcription_payload_paragraphs_fallback():
    rec = {"sample_id": "s1", "audio": {"path": "a.wav"},
           "transcription": {"status": "ok", "text": "",
                             "paragraphs": [{"text": "第一段内容"},
                                            {"text": "第二段内容"}]}}
    d = inference.extract_candidate(rec, _cfg(include_transcription_payload=True))
    assert d["status"] == "described"
    assert d["description"] == "第一段内容第二段内容"


def test_include_transcription_payload_must_be_bool():
    from utils.schemas import ConfigError, _with_defaults, validate_config
    cfg = _with_defaults({"description": {"include_transcription_payload": "yes"}})
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_frozen_priority_constant():
    assert inference.SOURCE_PRIORITY == FROZEN


# --------------------------------------------------------------------------
# 2. 长度边界：最短 5、最长 500、超长截断
# --------------------------------------------------------------------------

def test_min_length_boundary_skip_to_next_source():
    rec = {"sample_id": "s1", "caption": "短",
           "alt": "这是一只金毛犬在草地上接飞盘的画面"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["description_source"] == "alt"
    assert any(s["source"] == "caption" and s["reason"] == "too_short" for s in d["skipped"])


def test_min_length_exact_five_is_valid():
    rec = {"sample_id": "s1", "caption": "一二三四五"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "described" and d["description_length"] == 5


def test_max_length_truncation_recorded():
    rec = {"sample_id": "s1", "caption": "描述内容" * 200}
    d = inference.extract_candidate(rec, _cfg())
    assert d["truncated"] is True
    assert d["description_length"] == 500
    assert d["original_length"] == 800


# --------------------------------------------------------------------------
# 3. 文件名 / 标签不能作为描述
# --------------------------------------------------------------------------

def test_filename_is_not_a_description():
    rec = {"sample_id": "s1", "caption": "img_0006", "image": "img_0006.png"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "pending_manual"
    assert any(s["reason"] == "equals_filename" for s in d["skipped"])


def test_filename_with_extension_is_not_a_description():
    rec = {"sample_id": "s1", "caption": "img_0006.png", "image": "img_0006.png"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "pending_manual"


def test_tag_is_not_a_description():
    rec = {"sample_id": "s1", "caption": "风景照片示例", "image": "img_0007.png",
           "metadata": {"image_tags": ["风景照片示例"]}}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "pending_manual"
    assert any(s["reason"] == "equals_tag" for s in d["skipped"])


# --------------------------------------------------------------------------
# 4. 无有效描述：不隔离、不造假、生成人工补写任务
# --------------------------------------------------------------------------

def test_bare_image_generates_manual_task_without_model():
    rec = {"sample_id": "s1", "image": "img_0008.png", "text": "一只小狗在玩耍"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "pending_manual"
    assert d["description"] is None
    # 禁止伪造模型输出
    assert "confidence" not in d and "score" not in d and "model" not in d


def test_run_bare_image_task_has_no_isolation_destination():
    r = inference.run(str(SAMPLE), "pytest_pending")
    tasks = [json.loads(l) for l in Path(r["rewriteFile"]).read_text(encoding="utf-8").splitlines()]
    assert tasks, "应生成人工补写任务"
    for t in tasks:
        assert t["action"] == "waiting_manual"
        assert t["destination"] == "pipeline_wait"
        assert t["model_used"] is None
        # [v4.2] status 双字段：int status=1（等待回写，不是失败）+ process_status
        assert t["status"] == 1
        assert t["process_status"] == "waiting_description"


# --------------------------------------------------------------------------
# 5. 样例端到端与统计守恒
# --------------------------------------------------------------------------

def test_run_on_sample_counts_and_conservation():
    r = inference.run(str(SAMPLE), "pytest_sample")
    assert r["status"] == "success"
    stats = r["statistics"]
    assert stats["described"] == 8
    assert stats["pending_manual"] == 3
    assert stats["bad_lines"] == 1
    assert stats["missing_sample_id"] == 1
    assert stats["truncated"] == 1
    assert stats["records"] == stats["described"] + stats["pending_manual"] + stats["missing_sample_id"]
    assert stats["total_lines"] == stats["described"] + stats["pending_manual"] + stats["rejected"]

    described_ids = [json.loads(l)["sample_id"]
                     for l in Path(r["outputFile"]).read_text(encoding="utf-8").splitlines()]
    assert described_ids == ["sample-0001", "sample-0002", "sample-0003",
                             "sample-0004", "sample-0005", "sample-0009",
                             "sample-0010", "sample-0011"]

    sources = {json.loads(l)["sample_id"]: json.loads(l)["description_source"]
               for l in Path(r["outputFile"]).read_text(encoding="utf-8").splitlines()}
    assert sources["sample-0001"] == "caption"
    assert sources["sample-0002"] == "alt"
    assert sources["sample-0003"] == "title"
    assert sources["sample-0004"] == "subtitle"
    assert sources["sample-0005"] == "transcript"
    assert sources["sample-0009"] == "alt"
    assert sources["sample-0010"] == "manual"

    tr = {json.loads(l)["sample_id"]: json.loads(l)["description_truncated"]
          for l in Path(r["outputFile"]).read_text(encoding="utf-8").splitlines()}
    assert tr["sample-0011"] is True and tr["sample-0001"] is False


def test_run_preserves_upstream_fields_and_writes_processing():
    r = inference.run(str(SAMPLE), "pytest_sample_preserve")
    rec = json.loads(Path(r["outputFile"]).read_text(encoding="utf-8").splitlines()[0])
    assert rec["sample_id"] == "sample-0001"
    assert rec["image"] == "img_0001.png"
    assert rec["processing"]["09"]["status"] == 1
    assert rec["processing"]["09"]["process_status"] == "success"
    assert rec["processing"]["09"]["metrics"]["description_source"] == "caption"


def test_output_is_utf8_without_escape():
    r = inference.run(str(SAMPLE), "pytest_utf8")
    raw = Path(r["outputFile"]).read_bytes()
    assert "金毛犬".encode("utf-8") in raw
    text = raw.decode("utf-8")
    assert "\\u91d1" not in text


# --------------------------------------------------------------------------
# 6. 异常：空文件、损坏 JSONL、错误编码、缺字段、文件不存在
# --------------------------------------------------------------------------

def test_empty_input(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    r = inference.run(str(empty), "pytest_empty")
    assert r["status"] == "success"
    assert r["statistics"]["total_lines"] == 0
    assert Path(r["outputFile"]).read_text(encoding="utf-8") == ""


def test_corrupt_json_line_is_not_swallowed(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"sample_id": "s1", "caption": "有效的一条描述内容"}\n{not json}\n',
                   encoding="utf-8")
    r = inference.run(str(bad), "pytest_bad")
    assert r["statistics"]["bad_lines"] == 1
    assert r["statistics"]["described"] == 1
    rej = [json.loads(l) for l in Path(r["rejectsFile"]).read_text(encoding="utf-8").splitlines()]
    assert rej[0]["reason"] == "json_parse_error"
    assert rej[0]["destination"] == "原始隔离区"


def test_wrong_encoding_line_rejected(tmp_path):
    bad = tmp_path / "encoding.jsonl"
    bad.write_bytes(b"\xff\xfe\x00\x00\x00\n")
    r = inference.run(str(bad), "pytest_encoding")
    assert r["status"] == "success"
    assert r["statistics"]["bad_lines"] == 1
    assert Path(r["rejectsFile"]).read_text(encoding="utf-8").strip() != ""


def test_missing_sample_id_is_rejected(tmp_path):
    f = tmp_path / "noid.jsonl"
    f.write_text('{"caption": "一条没有 sample_id 的描述文本"}\n', encoding="utf-8")
    r = inference.run(str(f), "pytest_noid")
    assert r["statistics"]["missing_sample_id"] == 1
    rej = json.loads(Path(r["rejectsFile"]).read_text(encoding="utf-8").splitlines()[0])
    assert rej["reason"].startswith("missing_field")


def test_input_not_found_failed_lineage():
    r = inference.run(str(BASE / "no_such.jsonl"), "pytest_missing")
    assert r["status"] == "failed"
    assert r["reason"] == "input_not_found"
    # [v4.2] 血缘输出已停用：run() 不再返回 lineage 键
    assert "lineage" not in r and "lineageFile" not in r


# --------------------------------------------------------------------------
# 7. 错误配置
# --------------------------------------------------------------------------

def test_config_min_length_out_of_range():
    with pytest.raises(ValueError):
        inference.load_config(overrides={"description": {"min_length": 4}})


def test_config_max_length_out_of_range():
    with pytest.raises(ValueError):
        inference.load_config(overrides={"description": {"max_length": 999}})


def test_config_priority_reorder_rejected(tmp_path):
    import yaml
    cfg = inference.load_config()
    cfg["description"]["priority"] = ["alt", "caption", "title", "subtitle", "transcript", "manual"]
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError):
        inference.load_config(str(p))


def test_config_unknown_override_key_rejected():
    with pytest.raises(ValueError):
        inference.load_config(overrides={"description": {"unknown_key": 1}})


# --------------------------------------------------------------------------
# 8. 任务隔离与路径安全
# --------------------------------------------------------------------------

def test_isolation_between_two_tasks():
    a = inference.run(str(SAMPLE), "pytest_iso_a")
    b = inference.run(str(SAMPLE), "pytest_iso_b")
    assert Path(a["outputFile"]).parent != Path(b["outputFile"]).parent
    assert Path(a["outputFile"]).is_file() and Path(b["outputFile"]).is_file()
    assert a["statistics"] == b["statistics"]


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "../x", "C:tmp", "CON"])
def test_illegal_unique_id_rejected(bad):
    with pytest.raises(ValueError):
        inference.task_dir(BASE / "output", bad)


def test_atomic_write_when_input_equals_output(tmp_path):
    """输入输出同路径时不得截断源文件。"""
    target = tmp_path / "same.jsonl"
    target.write_text('{"sample_id": "s1", "caption": "一条有效的候选描述文本"}\n',
                      encoding="utf-8")
    r = inference.run(str(target), "pytest_same", output_root=str(tmp_path))
    # run 写入 output_root/<uid>/，重新指向同路径再验证原子替换语义
    task_out = Path(r["outputFile"])
    assert task_out.is_file()
    import utils.io as uio
    uio.write_jsonl_atomic(task_out, [{"sample_id": "s1", "caption": "替换后仍可读取"}])
    content = json.loads(task_out.read_text(encoding="utf-8").splitlines()[0])
    assert content["caption"] == "替换后仍可读取"
    # 源文件保持完整
    assert "一条有效的候选描述文本" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 10. 描述质量（v1.0.1）：过短择优 + 语言标记纠正
# --------------------------------------------------------------------------

def test_short_caption_prefers_longer_alternate():
    rec = {"sample_id": "s1",
           "caption": "A dog barks .",
           "metadata": {"alt_candidates": [
               "A dog barks .",
               "A brown dog barks loudly at a man in a kitchen .",
               "A dog is barking ."]}}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "described"
    assert d["description"] == "A brown dog barks loudly at a man in a kitchen ."
    assert d["alternate_used"] is True
    assert d["alternate_index"] == 1
    assert d["word_count"] >= 6
    assert d["quality"] == "ok"


def test_short_caption_without_alternate_is_flagged_not_blocked():
    rec = {"sample_id": "s1", "caption": "A dog barks ."}
    d = inference.extract_candidate(rec, _cfg())
    assert d["status"] == "described"          # 不因过短进入待补写
    assert d["description"] == "A dog barks ."
    assert d["quality"] == "short"
    assert d["alternate_used"] is False


def test_alternate_must_obey_filename_and_tag_rules():
    rec = {"sample_id": "s1", "caption": "A dog barks .",
           "image": "media/a_dog_barks.jpg",
           "metadata": {"alt_candidates": ["media/a_dog_barks.jpg"]}}
    d = inference.extract_candidate(rec, _cfg())
    assert d["description"] == "A dog barks ."
    assert d["alternate_used"] is False


def test_min_words_zero_disables_short_handling():
    rec = {"sample_id": "s1", "caption": "A dog barks .",
           "metadata": {"alt_candidates": ["A brown dog barks loudly ."]}}
    d = inference.extract_candidate(rec, _cfg(min_words=0))
    assert d["description"] == "A dog barks ."
    assert d["quality"] == "ok"


def test_prefer_alternate_can_be_disabled():
    rec = {"sample_id": "s1", "caption": "A dog barks .",
           "metadata": {"alt_candidates": ["A brown dog barks loudly ."]}}
    d = inference.extract_candidate(rec, _cfg(prefer_longer_alternate=False))
    assert d["description"] == "A dog barks ."
    assert d["quality"] == "short"


def test_description_language_detected_for_english_caption():
    rec = {"sample_id": "s1", "caption": "A man riding a wave on top of a surfboard ."}
    d = inference.extract_candidate(rec, _cfg())
    assert d["language"] == "en"
    assert d["lang_source"] == "detected"


def test_description_language_detected_for_chinese_caption():
    rec = {"sample_id": "s1", "caption": "一名男子在浪尖上冲浪"}
    d = inference.extract_candidate(rec, _cfg())
    assert d["language"] == "zh"


def test_run_fixes_mislabeled_text_language(tmp_path):
    src = tmp_path / "in.jsonl"
    src.write_text(json.dumps({
        "sample_id": "s1",
        "caption": "A man riding a wave on top of a surfboard .",
        "text": {"content": "A man riding a wave on top of a surfboard .", "lang": "zh"},
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    result = inference.run(str(src), "t1_f1", output_root=str(tmp_path / "out"))
    rows = [json.loads(line) for line in
            Path(result["outputFile"]).read_text(encoding="utf-8").splitlines()]
    row = rows[0]
    assert row["description_lang"] == "en"
    assert row["text"]["lang"] == "en"
    assert row["text"]["lang_declared"] == "zh"
    stats = result["statistics"]
    assert stats["description_lang_counts"].get("en") == 1
    assert "description_short" in stats


def test_detect_language_flag_false_keeps_declared_only():
    # 关闭检测后只回显记录声明的语言，不做脚本判定、也不纠正 text.lang
    rec = {"sample_id": "s1", "caption": "A man riding a wave on top of a surfboard .",
           "metadata": {"language": "zh"}}
    cfg = _cfg()
    cfg["description"]["detect_language"] = False
    d = inference.extract_candidate(rec, cfg)
    assert d["language"] == "zh"
    assert d["lang_source"] == "declared"


def test_language_detection_unit():
    from utils.language import detect_language, word_count

    assert detect_language("A dog is barking") == "en"
    assert detect_language("一只金毛犬在草地上接飞盘") == "zh"
    assert detect_language("これはテストです") == "ja"
    assert detect_language("이것은 테스트입니다") == "ko"
    assert detect_language("12345") == "und"
    assert detect_language("") == "und"
    assert word_count("A brown dog barks loudly") == 5
    assert word_count("一只金毛犬") == 5


def test_config_min_words_out_of_range():
    with pytest.raises(inference.ConfigError):
        inference.load_config(overrides={"description": {"min_words": 51}})
    with pytest.raises(inference.ConfigError):
        inference.load_config(overrides={"description": {"prefer_longer_alternate": "yes"}})
