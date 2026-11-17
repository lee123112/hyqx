"""ZIP 打包模块测试：真实压缩、条目一致、确定性、原子提交、重复路径拒绝。

另含交付包离线校验工具（`utils/prepare_package.py`）的正反用例。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from utils import archive


def _entries() -> list:
    samples = (json.dumps({"sample_id": "cm-0001", "token": "<image> 猫 <image>"},
                          ensure_ascii=False) + "\n").encode("utf-8")
    return [("train/samples.jsonl", samples),
            ("materials/quality_report.json", b'{"status": "written_back"}\n'),
            ("media/cm-0001.png", b"\x89PNG\r\n\x1a\nbinary-payload")]


def test_build_package_writes_real_zip(tmp_path):
    target = tmp_path / "pkg.zip"
    report = archive.build_package(target, _entries())
    assert target.is_file()
    assert report["entries"] == 3
    assert report["bytes"] == target.stat().st_size
    assert report["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert report["names"] == ["train/samples.jsonl",
                               "materials/quality_report.json",
                               "media/cm-0001.png"]
    with zipfile.ZipFile(target) as bundle:
        assert bundle.namelist() == report["names"]
        assert bundle.read("media/cm-0001.png") == b"\x89PNG\r\n\x1a\nbinary-payload"
        text = bundle.read("train/samples.jsonl").decode("utf-8")
    assert "cm-0001" in text and "猫" in text


def test_build_package_is_deterministic_for_same_timestamp(tmp_path):
    first = archive.build_package(tmp_path / "a.zip", _entries(),
                                  timestamp=(2026, 10, 8, 12, 0, 0))
    second = archive.build_package(tmp_path / "b.zip", _entries(),
                                   timestamp=(2026, 10, 8, 12, 0, 0))
    assert first["sha256"] == second["sha256"]


def test_build_package_uses_compression(tmp_path):
    payload = b"a" * 20000
    report = archive.build_package(tmp_path / "c.zip",
                                   [("train/samples.jsonl", payload)])
    assert report["bytes"] < len(payload)


def test_build_package_rejects_duplicate_paths(tmp_path):
    with pytest.raises(archive.ArchiveError):
        archive.build_package(tmp_path / "d.zip",
                              [("train/samples.jsonl", b"a"),
                               ("train/samples.jsonl", b"b")])


@pytest.mark.parametrize("name", ["../escape.jsonl", "/abs.jsonl", "C:/x.jsonl",
                                  "a\\b.jsonl", ""])
def test_build_package_rejects_unsafe_member_paths(tmp_path, name):
    with pytest.raises(archive.ArchiveError):
        archive.build_package(tmp_path / "e.zip", [(name, b"a")])


def test_build_package_leaves_no_partial_file(tmp_path):
    target = tmp_path / "f.zip"
    archive.build_package(target, _entries())
    leftovers = [item.name for item in tmp_path.iterdir() if item.name != "f.zip"]
    assert leftovers == []


def test_build_package_failure_keeps_previous_package(tmp_path):
    """写包失败不能破坏已有包（原子替换语义）。"""
    target = tmp_path / "g.zip"
    archive.build_package(target, _entries())
    before = target.read_bytes()
    with pytest.raises(archive.ArchiveError):
        archive.build_package(target, [("train/samples.jsonl", b"a"),
                                       ("train/samples.jsonl", b"b")])
    assert target.read_bytes() == before


def test_sha256_bytes_matches_hashlib():
    assert archive.sha256_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest()


# --------------------------------------------------------------------------- #
# 离线校验工具（工程侧交接用）
# --------------------------------------------------------------------------- #

def _package_members(bundle: zipfile.ZipFile) -> dict:
    return {name: bundle.read(name) for name in bundle.namelist()}


def test_prepare_package_verify_accepts_real_package(config_file, output_root, tmp_path):
    import prepare_package

    from conftest import build_scenario
    import inference

    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="verify_ok",
                           config_path=str(config_file), output_root=str(output_root))
    ok, problems = prepare_package.verify(Path(result["packagePath"]))
    assert ok is True, problems


def test_prepare_package_verify_detects_tampering(config_file, output_root, tmp_path):
    import prepare_package

    from conftest import build_scenario
    import inference

    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="verify_tamper",
                           config_path=str(config_file), output_root=str(output_root))
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(result["packagePath"]) as bundle:
        members = _package_members(bundle)
    members["train/samples.jsonl"] = members["train/samples.jsonl"] + b"{}\n"
    with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in members.items():
            bundle.writestr(name, payload)
    ok, problems = prepare_package.verify(tampered)
    assert ok is False
    assert any("sha256" in item for item in problems), problems


def test_prepare_package_verify_detects_isolated_sample_leak(config_file, output_root,
                                                             tmp_path):
    import prepare_package

    from conftest import build_scenario
    import inference

    entry = build_scenario(tmp_path / "task", written_back=True)
    result = inference.run(input_file=str(entry), unique_id="verify_leak",
                           config_path=str(config_file), output_root=str(output_root))
    leaky = tmp_path / "leaky.zip"
    with zipfile.ZipFile(result["packagePath"]) as bundle:
        members = _package_members(bundle)
    members["train/samples.jsonl"] += (
        b'{"sample_id": "cm-0013", "token": "<image> leaked <image>"}\n')
    with zipfile.ZipFile(leaky, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in members.items():
            bundle.writestr(name, payload)
    ok, problems = prepare_package.verify(leaky)
    assert ok is False
    assert any("隔离样本" in item for item in problems), problems
