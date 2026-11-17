"""算子 #12 run()/CLI 端到端测试：产物、统计守恒、血缘、任务隔离与降级。

用测试替身控制分数；真实 CLIP 权重缺失时的 degraded/failed 分支单独验证。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import inference


@pytest.fixture(autouse=True)
def _force_no_model_weights(tmp_path, monkeypatch):
    """本用例集按「无权重环境」编写（显式降级/显式失败）；把 MODEL_ROOT 指向空目录，
    使降级断言在已挂载权重的机器上同样可复现。业务断言不变。"""
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "_no_model_weights"))
  # noqa: E402
from conftest import StubEncoder, unit, vector_with_cosine, write_jsonl, write_png  # noqa: E402
from utils import scoring  # noqa: E402

TEXT_A = "一只金毛犬在草地上接飞盘"
TEXT_B = "红色方块放在桌面上"
PAIR_TEMPLATE = {
    "pair_id": "p1",
    "group_id": "grp-src-A",
    "modality_pair": ["text", "image"],
    "evidence": {"type": "A", "criterion": "same_source_id", "evidence_source": "rule",
                 "value": {"source_id": "src-A"}},
    "evidence_priority": 1,
    "left": {"sample_id": "s1", "modalities": ["text"],
             "record": {"sample_id": "s1", "source_id": "src-A", "batch_id": "batch-01",
                        "text": {"content": TEXT_A, "description_source": "caption"},
                        "image": None, "video": None, "audio": None, "metadata": {}}},
    "right": {"sample_id": "s2", "modalities": ["image"],
              "record": {"sample_id": "s2", "source_id": "src-A", "batch_id": "batch-01",
                         "text": None, "image": {"path": "media/a.png"}, "video": None,
                         "audio": None, "metadata": {}}},
    "producer": {"operator": "generate_candidate_pairs", "operator_version": "v1.0.0"},
}


def _pair(pair_id: str = "p1", image: str = "media/a.png", text: str = TEXT_A) -> dict:
    import copy

    payload = copy.deepcopy(PAIR_TEMPLATE)
    payload["pair_id"] = pair_id
    payload["left"]["record"]["text"] = {"content": text, "description_source": "caption"}
    payload["right"]["record"]["image"] = {"path": image}
    return payload


def _encoder() -> StubEncoder:
    return StubEncoder(
        text_vectors={TEXT_A: [1.0, 0.0], TEXT_B: [1.0, 0.0]},
        image_vectors={"a.png": [1.0, 0.0], "b.png": vector_with_cosine(0.25),
                       "c.png": unit([-1.0, 0.0])},
    )


def _fixture(tmp_path: Path, rows) -> Path:
    write_png(tmp_path / "media" / "a.png")
    write_png(tmp_path / "media" / "b.png")
    write_png(tmp_path / "media" / "c.png")
    return write_jsonl(tmp_path / "pairs.jsonl", rows)


def test_run_success_writes_scored_outputs(tmp_path):
    rows = [_pair("p1"), _pair("p2", image="media/b.png", text=TEXT_B),
            _pair("p3", image="media/c.png")]
    source = _fixture(tmp_path, rows)
    result = inference.run(str(source), "cli_case", output_root=str(tmp_path / "out"),
                          encoder=_encoder())
    assert result["status"] == "success"
    assert result["statistics"]["input"] == 3
    assert result["statistics"]["output"] == 2
    assert result["statistics"]["isolated"] == 1
    assert result["statistics"]["conservation_ok"] is True
    assert result["statistics"]["degraded"] is False

    main = [json.loads(line) for line in Path(result["outputFile"]).read_text("utf-8").splitlines()]
    assert len(main) == 2
    tiers = sorted(item["image_text_score"]["tier"] for item in main)
    assert tiers == [scoring.TIER_AUTO_ALIGNED, scoring.TIER_PENDING_MANUAL]

    isolated = [json.loads(line) for line in
                Path(result["isolationFile"]).read_text("utf-8").splitlines()]
    assert isolated[0]["pair_id"] == "p3"
    assert isolated[0]["label"] == "样本-语义不通"
    assert isolated[0]["destination"] == "样本隔离库"
    assert isolated[0]["evidence"]["clip_score"] == pytest.approx(-1.0)

    manifest = json.loads((Path(result["runDir"]) / "manifest.json").read_text("utf-8"))
    assert manifest["entrypoint"] == inference.MAIN_NAME
    roles = {item["role"] for item in manifest["artifacts"]}
    assert {"main", "sidecar", "review", "media"} <= roles

    # [v4.2] 血缘停用：run() 不再返回 lineage 节点（代码注释保留、暂不删除），血缘字段断言随之调整
    assert "lineage" not in result
    assert "stageLineage" not in result
    assert result["operator"] == inference.OPERATOR
    assert result["stageId"] == "S3"


def test_run_keeps_upstream_fields_and_utf8(tmp_path):
    source = _fixture(tmp_path, [_pair("p1")])
    result = inference.run(str(source), "utf8_case", output_root=str(tmp_path / "out"),
                          encoder=_encoder())
    raw = Path(result["outputFile"]).read_bytes()
    assert "一只金毛犬".encode("utf-8") in raw
    item = json.loads(raw.decode("utf-8").splitlines()[0])
    assert item["producer"]["operator"] == "generate_candidate_pairs"
    assert item["left"]["record"]["sample_id"] == "s1"
    assert item["scoring"]["operator"] == "#12"


def test_run_empty_input(tmp_path):
    source = write_jsonl(tmp_path / "pairs.jsonl", [])
    result = inference.run(str(source), "empty_case", output_root=str(tmp_path / "out"),
                          encoder=_encoder())
    assert result["status"] == "success"
    assert result["statistics"]["empty_input"] is True
    assert result["statistics"]["conservation_ok"] is True


def test_run_handles_bad_lines_and_encoding_errors(tmp_path):
    write_png(tmp_path / "media" / "a.png")
    payload = (json.dumps(_pair("p1"), ensure_ascii=False) + "\n").encode("utf-8")
    payload += b"{broken json}\n"
    payload += b"\xff\xfe not utf8\n"
    payload += (json.dumps({"left": {}, "right": {}}, ensure_ascii=False) + "\n").encode("utf-8")
    source = tmp_path / "pairs.jsonl"
    source.write_bytes(payload)

    result = inference.run(str(source), "bad_case", output_root=str(tmp_path / "out"),
                          encoder=_encoder())
    assert result["statistics"]["input"] == 4
    assert result["statistics"]["bad_lines"] == 2
    assert result["statistics"]["structural_rejects"] == 1
    assert result["statistics"]["output"] == 1
    assert result["statistics"]["conservation_ok"] is True
    reasons = {item["reason"] for item in
               [json.loads(line) for line in
                Path(result["rejectsFile"]).read_text("utf-8").splitlines()]}
    assert {"json_parse_error", "encoding_error", "missing_field: pair_id"} <= reasons


def test_run_degraded_when_model_unavailable(tmp_path):
    source = _fixture(tmp_path, [_pair("p1")])
    result = inference.run(str(source), "degraded_case", output_root=str(tmp_path / "out"))
    assert result["status"] == "degraded"
    assert result["statistics"]["degraded"] is True
    assert result["statistics"]["output"] == 0
    assert result["statistics"]["pending_model"] == 1
    assert result["statistics"]["conservation_ok"] is True
    # [v4.2] 血缘停用：降级态改从 statistics.model 核对
    assert result["statistics"]["model"]["degraded"] is True
    pending = [json.loads(line) for line in
               Path(result["pendingModelFile"]).read_text("utf-8").splitlines()]
    assert pending[0]["reason"] == "model_unavailable"


def test_run_fails_when_model_unavailable_and_degradation_forbidden(tmp_path):
    source = _fixture(tmp_path, [_pair("p1")])
    config = {"model": {"allow_degraded": False}}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    result = inference.run(str(source), "strict_case", config_path=str(cfg_path),
                          output_root=str(tmp_path / "out"))
    assert result["status"] == "failed"
    assert "model" in result["reason"]
    # [v4.2] 血缘停用：run() 失败返回不再携带 lineage 节点
    assert "lineage" not in result


def test_run_failed_on_config_error(tmp_path):
    source = _fixture(tmp_path, [_pair("p1")])
    result = inference.run(str(source), "cfg_case", output_root=str(tmp_path / "out"),
                          overrides={"isolation_threshold": 0.9})
    assert result["status"] == "failed"
    assert result["reason"].startswith("config_error")


def test_run_failed_when_input_missing(tmp_path):
    result = inference.run(str(tmp_path / "nope.jsonl"), "missing_case",
                          output_root=str(tmp_path / "out"))
    assert result["status"] == "failed"
    assert result["reason"] == "input_not_found"


def test_run_isolates_two_unique_ids(tmp_path):
    source = _fixture(tmp_path, [_pair("p1")])
    out = tmp_path / "out"
    first = inference.run(str(source), "task_a", output_root=str(out), encoder=_encoder())
    second = inference.run(str(source), "task_b", output_root=str(out), encoder=_encoder())
    assert first["runDir"] != second["runDir"]
    assert (out / "task_a").is_dir() and (out / "task_b").is_dir()
    assert Path(first["outputFile"]).is_file()
    assert Path(second["outputFile"]).is_file()
    assert (out / "task_a" / "pairs.jsonl").exists() is False


def test_run_repeat_creates_new_run_dir(tmp_path):
    source = _fixture(tmp_path, [_pair("p1")])
    out = tmp_path / "out"
    first = inference.run(str(source), "repeat_f1", output_root=str(out), encoder=_encoder())
    second = inference.run(str(source), "repeat_f1", output_root=str(out), encoder=_encoder())
    assert first["runId"] == "run001"
    assert second["runId"] == "run002"
    assert len([p for p in (out / "repeat_f1" / inference.OPERATOR).iterdir()
                if p.is_dir()]) == 2


def test_run_refuses_to_overwrite_input(tmp_path):
    out = tmp_path / "out"
    run_dir = out / "conflict_f1" / inference.OPERATOR / "run001"
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / inference.MAIN_NAME
    target.write_text(json.dumps(_pair("p1"), ensure_ascii=False) + "\n", encoding="utf-8")
    before = target.read_bytes()

    result = inference.run(str(target), "conflict_f1", output_root=str(out),
                          run_id="run001", encoder=_encoder())
    assert result["status"] == "failed"
    assert result["reason"] == "output_conflicts_with_input"
    assert target.read_bytes() == before


def test_run_records_model_identity_and_thresholds(tmp_path):
    source = _fixture(tmp_path, [_pair("p1")])
    result = inference.run(str(source), "identity_f1", output_root=str(tmp_path / "out"),
                          encoder=_encoder(), overrides={"isolation_threshold": 0.30,
                                                         "auto_align_threshold": 0.40})
    stats = result["statistics"]
    assert stats["thresholds"]["isolation"] == pytest.approx(0.30)
    assert stats["thresholds"]["auto_align"] == pytest.approx(0.40)
    assert stats["model"]["test_double"] is True
    assert result["config"]["image_text_scoring"]["isolation_threshold"] == pytest.approx(0.30)


def test_cli_returns_zero_on_success(tmp_path, capsys):
    source = _fixture(tmp_path, [_pair("p1")])
    exit_code = inference.main(["--input", str(source), "--unique-id", "cli_ok",
                                "--output-root", str(tmp_path / "out")])
    assert exit_code == 0
    assert "degraded" in capsys.readouterr().out


def test_cli_returns_one_on_invalid_unique_id(tmp_path, capsys):
    source = _fixture(tmp_path, [_pair("p1")])
    exit_code = inference.main(["--input", str(source), "--unique-id", "../escape",
                                "--output-root", str(tmp_path / "out")])
    assert exit_code == 1
    assert "failed" in capsys.readouterr().out
