"""真实 HTTP smoke：启动 uvicorn 子进程，走完三接口与阶段中间/阶段末两种形态。

用法：``python tests/smoke_api.py [--port 8108]``
退出码 0 表示全部通过；任何断言失败返回非 0。

只使用仓库内样例（``dataset/sample``），不下载外部数据，不访问外网。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

OPERATOR_DIR = Path(__file__).resolve().parents[1]
SAMPLE_DIR = OPERATOR_DIR / "dataset" / "sample"


def http_json(url: str, payload=None, timeout: float = 120.0, method=None):
    """GET（payload 为空）或 POST JSON。removeFile 只带查询参数，用 method=None+payload={}。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    verb = method or ("POST" if data is not None else "GET")
    request = urllib.request.Request(url, data=data, method=verb)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def http_multipart(url: str, archive: Path, timeout: float = 120.0):
    boundary = "----op02smoke"
    body = bytearray()
    body.extend(("--%s\r\n" % boundary).encode())
    body.extend(b'Content-Disposition: form-data; name="file"; filename="bundle.zip"\r\n')
    body.extend(b"Content-Type: application/zip\r\n\r\n")
    body.extend(archive.read_bytes())
    body.extend(("\r\n--%s--\r\n" % boundary).encode())
    request = urllib.request.Request(url, data=bytes(body), method="POST")
    request.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def http_error(url: str, payload=None, timeout: float = 60.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def build_bundle(target: Path) -> Path:
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(SAMPLE_DIR.rglob("*")):
            if path.is_file():
                handle.write(str(path), str(path.relative_to(SAMPLE_DIR)).replace("\\", "/"))
    return target


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError("smoke 断言失败：%s" % label)
    print("  [ok] %s" % label)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8108)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    base = "http://%s:%d" % (args.host, args.port)
    tmp = Path(tempfile.mkdtemp(prefix="op02_smoke_"))
    output_root = tmp / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["OUTPUT_DIR"] = str(output_root)
    env["PYTHONPATH"] = str(OPERATOR_DIR)

    archive = build_bundle(tmp / "bundle.zip")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", args.host,
         "--port", str(args.port), "--log-level", "warning"],
        cwd=str(OPERATOR_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 40
        ready = False
        while time.time() < deadline:
            try:
                status, _ = http_json(base + "/openapi.json", timeout=3)
                ready = status == 200
                break
            except Exception:  # noqa: BLE001 - 启动期重试
                time.sleep(0.5)
        if not ready:
            raise AssertionError("uvicorn 未能在 40s 内就绪")

        print("== 接口注册 ==")
        _status, spec = http_json(base + "/openapi.json")
        check(set(spec["paths"]) == {"/uploadFile", "/predict", "/removeFile"},
              "只注册三个业务接口（无 /downloadFile）")
        _code, body = http_error(base + "/downloadFile?uniqueId=x", {})
        check(body["code"] == 1 and _code == 404, "/downloadFile 返回 404 且信封 code=1")

        print("== 上传两个任务（隔离）==")
        status_a, upload_a = http_multipart(base + "/uploadFile?uniqueId=smoke_a", archive)
        check(status_a == 200 and upload_a["code"] == 0, "uniqueId=smoke_a 上传成功")
        status_b, upload_b = http_multipart(base + "/uploadFile?uniqueId=smoke_b", archive)
        check(status_b == 200 and upload_b["code"] == 0, "uniqueId=smoke_b 上传成功")
        check(upload_a["data"]["entrypoint"] == "demo.jsonl", "入口为 demo.jsonl")

        print("== 阶段首（不传 inputFile）+ 中间形态 ==")
        _code, mid = http_json(base + "/predict", {
            "uniqueId": "smoke_a", "isLastOperator": False,
            "config": {"enabled": True, "scene_id_source": "generate",
                       "min_modalities": 2}})
        check(mid["code"] == 0, "中间调用 code=0")
        check(set(mid["data"]) == {"lineageFile"}, "中间调用 data 只含 lineageFile")
        node = mid["data"]["lineageFile"]
        check(Path(node["outputFile"]).is_file(), "中间响应含临时 outputFile 且文件真实存在")
        stats = node["statistics"]
        check(stats["input_lines"] == 16 and stats["main_entries"] == 11,
              "真实计数：input_lines=16 / main_entries=11")
        check(stats["scene_groups_total"] == 8 and stats["verified_groups"] == 5,
              "补编号模式真实场景组：8 组（5 验证通过 / 3 模态不足）")
        check(stats["supplemented_scene_ids"] == 4, "补编号记录 4 条（只绑定一次）")
        check(stats["conservation_ok"] is True, "守恒校验通过")
        check(node["statistics"]["origin_rejected"] == 3
              and node["statistics"]["duplicate_records"] == 1,
              "原始隔离 3 行 / 重复标识 1 条")

        print("== 阶段内链式续跑（以阶段末形态收口，核验血缘累积）==")
        _code, chained = http_json(base + "/predict", {
            "uniqueId": "smoke_a", "isLastOperator": True,
            "inputFile": node["outputFile"],
            "config": {"enabled": True, "scene_id_source": "generate"}})
        check(chained["code"] == 0, "链式续跑 code=0")
        chain_stage = chained["data"]["lineageFile"]
        check(isinstance(chain_stage, list) and len(chain_stage) == 2,
              "阶段血缘累积为 2 条节点（同阶段内链式续跑）")
        check(all(item["uniqueId"] == "smoke_a" for item in chain_stage),
              "阶段血缘只含本任务")

        print("== 阶段末形态 ==")
        _code, final = http_json(base + "/predict", {
            "uniqueId": "smoke_b", "isLastOperator": True,
            "config": {"enabled": True}})
        check(final["code"] == 0, "阶段末 code=0")
        check(set(final["data"]) == {"outputFile", "lineageFile"}, "阶段末 data 两键")
        payload = final["data"]["outputFile"]
        check(payload["format"] == "crossmodal-files-v1"
              and payload["entrypoint"] == "02_scene_verified.jsonl",
              "阶段末返回 crossmodal-files-v1 文件内容对象")
        files = {item["path"] for item in payload["files"]}
        check({"02_scene_verified.jsonl", "02_scene_groups.jsonl",
               "02_routed_to_pairing.jsonl", "02_origin_quarantine.jsonl",
               "02_rejects.jsonl", "02_scene_report.json",
               "manifest.json"} <= files, "文件内容对象含全部产物")
        stage = final["data"]["lineageFile"]
        check(isinstance(stage, list) and "outputFile" not in stage[0],
              "阶段末血缘为数组且已移除临时 outputFile")

        print("== 非法路径与参数 ==")
        _code, evil = http_error(base + "/predict",
                                 {"uniqueId": "../escape", "isLastOperator": False})
        check(evil["code"] == 1 and _code == 422, "跨目录 uniqueId 被拒绝")
        _code, badbool = http_error(base + "/predict",
                                    {"uniqueId": "smoke_a", "isLastOperator": "false"})
        check(badbool["code"] == 1 and _code == 422, "非严格布尔 isLastOperator 被拒绝")
        _code, ro = http_error(base + "/predict", {
            "uniqueId": "smoke_a", "isLastOperator": False,
            "config": {"bind_once": False}})
        check(ro["code"] == 1, "表1 只读项 bind_once 不可覆盖")
        _code, traversal = http_error(base + "/predict", {
            "uniqueId": "smoke_b", "isLastOperator": False, "inputFile": "../../x.jsonl"})
        check(traversal["code"] == 1, "inputFile 目录穿越被拒绝")

        print("== 任务隔离与清理 ==")
        check((output_root / "smoke_a").is_dir() and (output_root / "smoke_b").is_dir(),
              "两个 uniqueId 各自独立任务目录")
        _code, removed = http_json(base + "/removeFile?uniqueId=smoke_a", {})
        check(removed["code"] == 0 and removed["data"]["removed"] is True, "removeFile 清理成功")
        check((output_root / "smoke_b").is_dir(), "清理 smoke_a 不影响 smoke_b")
        _code, again = http_json(base + "/removeFile?uniqueId=smoke_a", {})
        check(again["code"] == 0 and again["data"]["removed"] is False,
              "removeFile 幂等（重复调用返回 removed=false）")
        print("\n全部 smoke 断言通过")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
