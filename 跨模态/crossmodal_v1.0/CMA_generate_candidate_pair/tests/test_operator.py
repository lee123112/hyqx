"""算子 #11 运行级测试：真实样例、异常清单、统计守恒、任务隔离、配置错误、媒体携带。"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import inference  # noqa: E402


def read_jsonl(path: Path) -> list:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(json.loads(line))
    return items


def run_sample(sample: Path, unique_id: str, tmp_root: Path, **kwargs) -> dict:
    return inference.run(str(sample), unique_id, output_root=str(tmp_root), **kwargs)


def test_sample_run_matches_frozen_expectations(sample_jsonl, tmp_path):
    result = run_sample(sample_jsonl, "sample_run", tmp_path)
    assert result["status"] == "success"
    stats = result["statistics"]
    assert stats["input_records"] == 13
    assert stats["bad_lines"] == 1
    assert stats["rejected_records"] == 4
    assert stats["valid_records"] == 8
    # 样例中的八条有效记录本身均有真实文本和媒体，优先各自产生同源候选。
    assert stats["paired_records"] == 8
    assert stats["unpaired_records"] == 0
    assert stats["pairs_total"] == 8
    assert stats["derived_views"] == 16
    assert stats["pairs_by_evidence"] == {"A": 8, "B": 0, "C": 0}
    assert stats["groups_total"] == 6
    assert stats["empty_input"] is False
    assert stats["conservation_ok"] is True
    assert stats["media_carried"] == 6
    assert stats["media_missing"] == 0
    # 与兄弟算子共用的记录级口径，以及本算子无模型后端的事实
    assert stats["input"] == 13
    assert stats["output"] == 8
    assert stats["rejected"] == 5
    assert stats["input"] == stats["output"] + stats["rejected"]
    assert stats["applicable"] == 8
    assert stats["not_applicable"] == 0
    assert stats["degraded"] is False
    assert stats["active_backend"] == "rule"


def test_sample_outputs_on_disk_conserved(sample_jsonl, tmp_path):
    result = run_sample(sample_jsonl, "sample_disk", tmp_path)
    pairs = read_jsonl(Path(result["outputFile"]))
    unpaired = read_jsonl(Path(result["unpairedFile"]))
    rejects = read_jsonl(Path(result["rejectsFile"]))

    assert len(pairs) == 8
    assert len(unpaired) == 0
    assert len(rejects) == 5
    assert result["statistics"]["rejects_total"] == 5

    ids = [item["pair_id"] for item in pairs]
    assert len(ids) == len(set(ids))
    for pair in pairs:
        assert pair["left"]["sample_id"] != pair["right"]["sample_id"]
        assert pair["evidence"]["type"] in {"A", "B", "C"}

    # 一对多拆分共享 group_id
    group_a = {p["group_id"] for p in pairs if p["evidence"]["type"] == "A"}
    assert group_a == {"grp-src-src-" + letter for letter in "ABCDEF"}
    assert {pair["origin_sample_id"] for pair in pairs} == {
        "sample-%04d" % index for index in range(1, 9)}
    # 异常清单保留物理行号与台账字段
    reasons = {item["reason"] for item in rejects}
    assert {"json_parse_error", "missing_field: sample_id", "no_modality",
            "duplicate_sample_id", "not_admitted"} <= reasons
    for item in rejects:
        assert item["line"] >= 1
        assert item["stage"] == "11"
        assert item["operator"] == "generate_candidate_pairs"
        assert item["retry_count"] == 0
        assert item["event_time"]


def test_manifest_and_media_carried(sample_jsonl, tmp_path):
    result = run_sample(sample_jsonl, "sample_manifest", tmp_path)
    run_dir = Path(result["runDir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "crossmodal-manifest-v1"
    assert manifest["entrypoint"] == "11_candidate_pairs.jsonl"
    roles = {item["role"] for item in manifest["artifacts"]}
    assert {"main", "sidecar", "review", "media"} <= roles
    for item in manifest["artifacts"]:
        assert not Path(item["path"]).is_absolute()
        assert (run_dir / item["path"]).is_file(), item["path"]
    # [v4.2] 血缘/阶段归档已停用：不再落盘 lineage.json / stage_context.json
    assert not (run_dir / "lineage.json").exists()
    assert not (run_dir / "stage_context.json").exists()
    assert (run_dir / "run.log").is_file()
    assert not list(run_dir.glob("*.tmp"))


@pytest.mark.skip(reason="v4.2 血缘输出已停用：run() 不再返回 lineage 节点")
def test_lineage_node_is_complete(sample_jsonl, tmp_path):
    result = run_sample(sample_jsonl, "sample_lineage", tmp_path)
    node = result["lineage"]
    for key in ("operator", "operator_version", "uniqueId", "stageId", "inputFile",
                "outputFile", "status", "startedAt", "finishedAt", "config",
                "statistics", "runId", "artifacts", "model"):
        assert key in node, key
    assert node["operator"] == "generate_candidate_pairs"
    assert node["uniqueId"] == "sample_lineage"
    assert node["status"] == "success"
    assert node["model"] is None
    assert node["statistics"]["pairs_total"] == 4
    assert Path(node["outputFile"]).is_file()
    assert node["runId"] == "run001"
    # artifacts 与兄弟算子一致：{path, role} 列表
    assert all(set(item) == {"path", "role"} for item in node["artifacts"])
    assert {item["role"] for item in node["artifacts"]} >= {"main", "sidecar", "review", "media"}


def test_input_file_is_not_modified(sample_jsonl, tmp_path):
    before = hashlib.sha256(sample_jsonl.read_bytes()).hexdigest()
    run_sample(sample_jsonl, "sample_immutable", tmp_path)
    assert hashlib.sha256(sample_jsonl.read_bytes()).hexdigest() == before


def test_empty_input_is_reported_not_crash(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    result = run_sample(empty, "empty_f1", tmp_path / "out")
    assert result["status"] == "success"
    stats = result["statistics"]
    assert stats["input_records"] == 0
    assert stats["empty_input"] is True
    assert stats["conservation_ok"] is True
    assert stats["pairs_total"] == 0
    assert read_jsonl(Path(result["outputFile"])) == []


def test_corrupt_lines_go_to_rejects(tmp_path):
    src = tmp_path / "bad.jsonl"
    src.write_text('{"sample_id": "s1", "source_id": "a", "text": {"content": "x"},'
                   ' "image": "i.png"}\n'
                   '{not json}\n'
                   '[1, 2, 3]\n', encoding="utf-8")
    result = run_sample(src, "corrupt_f1", tmp_path / "out")
    assert result["statistics"]["bad_lines"] == 2
    assert result["statistics"]["valid_records"] == 1
    reasons = {item["reason"] for item in read_jsonl(Path(result["rejectsFile"]))}
    assert {"json_parse_error", "record_not_object"} <= reasons


def test_encoding_error_is_recorded(tmp_path):
    src = tmp_path / "bad_encoding.jsonl"
    src.write_bytes(b'{"sample_id": "ok", "source_id": "a", "text": {"content": "x"},'
                    b' "image": "i.png"}\n' + b'\xff\xfe{"sample_id": "broken"}\n')
    result = run_sample(src, "encoding_f1", tmp_path / "out")
    stats = result["statistics"]
    assert stats["bad_lines"] == 1
    rejects = read_jsonl(Path(result["rejectsFile"]))
    assert any(item["reason"] == "encoding_error" for item in rejects)
    assert rejects[0]["label"] == "原始文件-编码异常"
    assert rejects[0]["destination"] == "原始隔离区"


def test_missing_field_and_no_modality(tmp_path):
    src = tmp_path / "fields.jsonl"
    src.write_text('{"source_id": "a", "text": {"content": "no id"}, "image": "i.png"}\n'
                   '{"sample_id": "s2", "source_id": "a"}\n', encoding="utf-8")
    result = run_sample(src, "fields_f1", tmp_path / "out")
    stats = result["statistics"]
    assert stats["rejected_records"] == 2
    assert stats["valid_records"] == 0
    reasons = [item["reason"] for item in read_jsonl(Path(result["rejectsFile"]))]
    assert reasons == ["missing_field: sample_id", "no_modality"]


def test_wrong_config_fails_without_fake_success(sample_jsonl, tmp_path):
    bad_cfg = tmp_path / "bad.yaml"
    bad_cfg.write_text(yaml.safe_dump({"operator": "generate_candidate_pairs",
                                       "pairing": {"enabled": True, "priority": "source",
                                                   "keyword_min_overlap": 1}}),
                       encoding="utf-8")
    result = inference.run(str(sample_jsonl), "bad_config", config_path=str(bad_cfg),
                           output_root=str(tmp_path / "out"))
    assert result["status"] == "failed"
    assert result["reason"].startswith("config_error")
    # [v4.2] 血缘停用：run() 失败返回不再携带 lineage 节点
    assert "lineage" not in result


def test_disabled_config_is_rejected(tmp_path, sample_jsonl):
    bad_cfg = tmp_path / "disabled.yaml"
    bad_cfg.write_text(yaml.safe_dump({"operator": "generate_candidate_pairs",
                                       "pairing": {"enabled": False}}), encoding="utf-8")
    result = inference.run(str(sample_jsonl), "disabled_f1", config_path=str(bad_cfg),
                           output_root=str(tmp_path / "out"))
    assert result["status"] == "failed"
    assert "enabled" in result["reason"]


def test_missing_input_file_fails(tmp_path):
    result = inference.run(str(tmp_path / "nope.jsonl"), "missing_in",
                           output_root=str(tmp_path / "out"))
    assert result["status"] == "failed"
    assert result["reason"] == "input_not_found"
    # [v4.2] 血缘停用：run() 失败返回不再携带 lineage 节点
    assert "lineage" not in result


def test_task_isolation_between_unique_ids(sample_jsonl, tmp_path):
    first = run_sample(sample_jsonl, "iso_a", tmp_path)
    second = run_sample(sample_jsonl, "iso_b", tmp_path)
    assert first["runDir"] != second["runDir"]
    assert "iso_a" in Path(first["runDir"]).parts
    assert "iso_b" in Path(second["runDir"]).parts
    assert not str(tmp_path / "iso_a") in second["runDir"]

    def business(path):
        return [{key: value for key, value in pair.items() if key != "producer"}
                for pair in read_jsonl(path)]

    assert business(Path(first["outputFile"])) == business(Path(second["outputFile"]))
    # [v4.2] 血缘停用：改由 runDir 任务段核对 uniqueId 隔离
    assert "iso_a" in Path(first["runDir"]).parts
    assert "iso_b" in Path(second["runDir"]).parts


def test_repeated_run_uses_new_run_id_and_keeps_previous(sample_jsonl, tmp_path):
    first = run_sample(sample_jsonl, "repeat_f1", tmp_path)
    second = run_sample(sample_jsonl, "repeat_f1", tmp_path)
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()


def test_illegal_unique_id_rejected(sample_jsonl, tmp_path):
    for bad in ("", "   ", ".", "..", "a/b", "a\\b", "C:evil", "CON"):
        with pytest.raises(ValueError):
            inference.run(str(sample_jsonl), bad, output_root=str(tmp_path))


def test_config_override_changes_result(tmp_path):
    # 用分离对象覆盖 C 级阈值；完整样本的同记录证据不受跨记录阈值影响。
    src = tmp_path / "separated.jsonl"
    src.write_text(json.dumps({"sample_id": "text", "text": {"content": "car street"}}) + "\n" +
                   json.dumps({"sample_id": "image", "image": "i.png",
                               "metadata": {"tags": ["car", "street"]}}) + "\n", encoding="utf-8")
    baseline = run_sample(src, "override_baseline", tmp_path)
    assert baseline["statistics"]["pairs_by_evidence"]["C"] == 1
    result = run_sample(src, "override_f1", tmp_path,
                        overrides={"keyword_min_overlap": 5})
    assert result["status"] == "success"
    stats = result["statistics"]
    assert stats["pairs_by_evidence"]["C"] == 0
    assert stats["unpaired_records"] >= 2
    # [v4.2] 血缘停用：改读 run() 返回的生效配置快照
    assert result["config"]["pairing"]["keyword_min_overlap"] == 5


def test_media_missing_is_counted_not_fabricated(tmp_path):
    src = tmp_path / "missing_media.jsonl"
    src.write_text('{"sample_id": "s1", "source_id": "a", "text": {"content": "x"},'
                   ' "image": "不存在.png"}\n'
                   '{"sample_id": "s2", "source_id": "a", "text": {"content": "y"},'
                   ' "image": "不存在.png"}\n', encoding="utf-8")
    result = run_sample(src, "media_missing", tmp_path / "out")
    stats = result["statistics"]
    assert stats["pairs_total"] == 2
    assert stats["media_missing"] == 1
    assert stats["media_carried"] == 0


def test_cli_smoke(tmp_path, sample_jsonl, capsys):
    code = inference.main(["--input", str(sample_jsonl), "--unique-id", "cli_smoke",
                           "--output-root", str(tmp_path / "out")])
    assert code == 0
    out = capsys.readouterr().out
    assert "success" in out and "pairs=8" in out


def test_cli_failure_returns_nonzero(tmp_path, capsys):
    code = inference.main(["--input", str(tmp_path / "nope.jsonl"),
                           "--unique-id", "cli_fail", "--output-root", str(tmp_path / "out")])
    assert code == 1


def test_run_dir_cleanup_keeps_other_tasks(sample_jsonl, tmp_path):
    run_sample(sample_jsonl, "keep_a", tmp_path)
    run_sample(sample_jsonl, "keep_b", tmp_path)
    shutil.rmtree(tmp_path / "keep_a")
    assert (tmp_path / "keep_b").is_dir()


def test_run_pairs_complete_admitted_records_on_each_route_and_counts_sources(tmp_path):
    records = []
    for media in ("image", "audio", "video"):
        for index in range(2):
            sample_id = "%s-%d" % (media, index)
            asset = tmp_path / (sample_id + ".bin")
            asset.write_bytes(b"fixture media")
            records.append({"sample_id": sample_id, "source_id": "source-" + sample_id,
                            "batch_id": "shared-batch", "admission": {"passed": True},
                            "license": "train", "text": {"content": "native caption"},
                            media: {"path": asset.name, "duration": 2}})
    rejected = dict(records[0], sample_id="not-admitted", admission={"passed": False})
    source = tmp_path / "native.jsonl"
    source.write_text("".join(json.dumps(record) + "\n" for record in records + [rejected]),
                      encoding="utf-8")
    before = source.read_bytes()
    result = run_sample(source, "native_routes", tmp_path / "out")
    pairs = read_jsonl(Path(result["outputFile"]))
    assert len(pairs) == 6
    assert [pair["modality_pair"] for pair in pairs].count(["text", "image"]) == 2
    assert [pair["modality_pair"] for pair in pairs].count(["text", "audio"]) == 2
    assert [pair["modality_pair"] for pair in pairs].count(["text", "video"]) == 2
    assert all(pair["evidence"]["type"] == "A" for pair in pairs)
    assert read_jsonl(Path(result["unpairedFile"])) == []
    stats = result["statistics"]
    assert stats["input_records"] == 7
    assert stats["valid_records"] == stats["paired_records"] == 6
    assert stats["derived_views"] == 12
    assert stats["in_record_pairs"] == 6
    assert stats["rejected_records"] == 1
    assert stats["conservation_ok"] is True
    assert source.read_bytes() == before


def test_run_in_record_routes_respect_group_limit_and_persist_excess_ledger(tmp_path):
    record = {"sample_id": "complete", "source_id": "source", "admission": {"passed": True},
              "text": {"content": "native caption"}, "image": "a.png",
              "video": "a.mp4", "audio": "a.wav"}
    for filename in ("a.png", "a.mp4", "a.wav"):
        (tmp_path / filename).write_bytes(b"fixture media")
    source = tmp_path / "native.jsonl"
    source.write_text(json.dumps(record) + "\n", encoding="utf-8")
    result = run_sample(source, "native_limit", tmp_path / "out",
                        overrides={"max_pairs_per_group": 1})
    pairs = read_jsonl(Path(result["outputFile"]))
    rejects = read_jsonl(Path(result["rejectsFile"]))
    assert [pair["modality_pair"] for pair in pairs] == [["text", "image"]]
    assert len(rejects) == 2
    for item in rejects:
        assert item["sample_id"] == "complete"
        assert item["reason"] == "group_pair_limit_exceeded"
        assert item["peer_sample_id"].endswith(("-video", "-audio"))
        assert item["group_level"] == "A"
        assert item["line"] == 1
    stats = result["statistics"]
    assert stats["valid_records"] == stats["paired_records"] == 1
    assert stats["pairs_total"] == stats["in_record_pairs"] == 1
    assert stats["derived_views"] == stats["group_pair_limit_hits"] == stats["rejects_total"] == 2
    assert stats["rejected_records"] == 0
    assert stats["conservation_ok"] is True
    assert read_jsonl(Path(result["unpairedFile"])) == []
