"""真实 HTTP smoke：启动 uvicorn 子进程 + httpx 打三个正式接口。

覆盖：上传/预测（阶段中间与阶段末两种形态）、任务隔离、非法 uniqueId、zip 穿越、
跨任务 inputFile、非法 isLastOperator、/downloadFile 不存在、removeFile 幂等、
以及开启路线的「显式降级（无分数）」与「严格失败」两种模型不可用行为。

用法::

    python tests/smoke_api.py

退出码 0 = 全部通过；非 0 = 存在失败项（会打印 FAIL 明细）。
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parents[1]
SAMPLE_DIR = BASE_DIR / "dataset" / "sample"
DEFAULT_PORT = int(os.environ.get("SMOKE_PORT", "8118"))

RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    print("%-4s %s%s" % ("PASS" if condition else "FAIL", name,
                         "" if condition else "  <-- %s" % detail))


def build_bundle() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("demo.jsonl", "demo_enabled.jsonl"):
            archive.writestr(name, (SAMPLE_DIR / name).read_bytes())
        for media in sorted((SAMPLE_DIR / "media").iterdir()):
            archive.writestr("media/%s" % media.name, media.read_bytes())
    return buffer.getvalue()


def free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_ready(client, url: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = client.get("%s/health" % url)
            if response.status_code == 200:
                return True
        except Exception:                      # noqa: BLE001 - 启动期间连接失败属正常
            time.sleep(0.5)
    return False


def main() -> int:
    import httpx

    work = Path(tempfile.mkdtemp(prefix="op18_smoke_"))
    output_dir = work / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = work / "config.yaml"
    config_path.write_text((BASE_DIR / "configs" / "config.yaml").read_text(encoding="utf-8"),
                           encoding="utf-8")

    port = free_port(DEFAULT_PORT)
    url = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env.update({"OUTPUT_DIR": str(output_dir), "OPERATOR_CONFIG": str(config_path),
                "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(BASE_DIR)})
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(BASE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    bundle = build_bundle()
    try:
        with httpx.Client(timeout=120.0) as client:
            ready = wait_ready(client, url)
            check("uvicorn 启动并就绪(/health)", ready, "未能连接 %s" % url)
            if not ready:
                return 1

            health = client.get("%s/health" % url).json()
            check("GET /health 返回信封且声明三接口",
                  health["code"] == 0 and health["data"]["downloadFile"] == "not_registered",
                  json.dumps(health, ensure_ascii=False)[:200])
            check("health 报告正式编号与默认阈值",
                  health["data"]["formal_operator_number"] == 18
                  and health["data"]["route_enabled"] is False,
                  json.dumps(health["data"], ensure_ascii=False)[:200])

            # ---- 上传 ---------------------------------------------------- #
            upload_a = client.post("%s/uploadFile?uniqueId=smoke_task_a" % url,
                                   files={"file": ("bundle.zip", bundle, "application/zip")})
            body_a = upload_a.json()
            check("POST /uploadFile 成功且给出 entrypoint",
                  upload_a.status_code == 200 and body_a["code"] == 0
                  and body_a["data"]["entrypoint"] == "demo.jsonl",
                  json.dumps(body_a, ensure_ascii=False)[:200])
            check("上传返回媒体清单",
                  "media/shot_001.png" in body_a["data"]["mediaFiles"],
                  str(body_a["data"]["mediaFiles"])[:200])

            upload_b = client.post("%s/uploadFile?uniqueId=smoke_task_b" % url,
                                   files={"file": ("bundle.zip", bundle, "application/zip")})
            check("上传第二个任务成功", upload_b.json()["code"] == 0)

            # ---- 阶段中间形态 -------------------------------------------- #
            mid = client.post("%s/predict" % url,
                              json={"uniqueId": "smoke_task_a", "isLastOperator": False})
            mid_body = mid.json()
            check("predict(中间) 只返回 lineageFile",
                  mid.status_code == 200 and mid_body["code"] == 0
                  and set(mid_body["data"].keys()) == {"lineageFile"},
                  json.dumps(mid_body, ensure_ascii=False)[:200])
            mid_node = mid_body["data"]["lineageFile"]
            check("中间节点含临时 outputFile 与算子标识",
                  bool(mid_node.get("outputFile"))
                  and mid_node["operator"] == "phrase_region_annotation"
                  and mid_node["formalOperatorNumber"] == 18,
                  json.dumps({k: mid_node.get(k) for k in
                              ("outputFile", "operator", "formalOperatorNumber")},
                             ensure_ascii=False)[:200])
            check("关闭路线：10 条原样放行、0 条细粒度对应",
                  mid_node["statistics"]["skipped_records"] == 10
                  and mid_node["statistics"]["relations_total"] == 0
                  and mid_node["statistics"]["deliverable_records"] == 0,
                  json.dumps(mid_node["statistics"], ensure_ascii=False)[:300])
            check("统计守恒校验通过", mid_node["statistics"]["conservation_ok"] is True)

            # ---- 阶段末形态 ---------------------------------------------- #
            end = client.post("%s/predict" % url,
                              json={"uniqueId": "smoke_task_a", "isLastOperator": True})
            end_body = end.json()
            check("predict(阶段末) 返回 outputFile + lineageFile",
                  end.status_code == 200 and set(end_body["data"].keys())
                  == {"outputFile", "lineageFile"},
                  json.dumps(list(end_body["data"].keys()))[:200])
            file_object = end_body["data"]["outputFile"]
            paths = {item["path"] for item in file_object["files"]}
            check("文件内容对象为 crossmodal-files-v1 且含清单与媒体",
                  file_object["format"] == "crossmodal-files-v1"
                  and file_object["entrypoint"] == "18_passthrough.jsonl"
                  and "manifest.json" in paths,
                  str(file_object.get("entrypoint"))[:200])
            check("阶段末血缘不含中间 outputFile",
                  isinstance(end_body["data"]["lineageFile"], list)
                  and "outputFile" not in end_body["data"]["lineageFile"][0],
                  json.dumps(end_body["data"]["lineageFile"][0], ensure_ascii=False)[:200])

            # ---- 任务隔离 ------------------------------------------------ #
            iso = client.post("%s/predict" % url,
                              json={"uniqueId": "smoke_task_b", "isLastOperator": False}).json()
            check("两个 uniqueId 的任务血缘互相隔离",
                  iso["data"]["lineageFile"]["uniqueId"] == "smoke_task_b"
                  and mid_node["uniqueId"] == "smoke_task_a",
                  json.dumps([mid_node["uniqueId"], iso["data"]["lineageFile"]["uniqueId"]]))

            # ---- 开启路线：显式降级（无任何分数） ------------------------ #
            degraded = client.post("%s/predict" % url,
                                   json={"uniqueId": "smoke_task_b", "isLastOperator": True,
                                         "overrides": {"enabled": True,
                                                       "allow_degraded": True}}).json()
            stage = degraded["data"]["lineageFile"][0]
            check("开启路线 + 显式降级：status=degraded 且不产生分数",
                  degraded["code"] == 0 and stage["statistics"]["degraded"] is True
                  and stage["statistics"]["relations_total"] == 0
                  and stage["statistics"]["model_unavailable"] > 0
                  and stage["statistics"]["conservation_ok"] is True,
                  json.dumps(stage["statistics"], ensure_ascii=False)[:300])

            # ---- 开启路线：严格失败（不伪造分数） ------------------------ #
            strict = client.post("%s/predict" % url,
                                 json={"uniqueId": "smoke_task_b", "isLastOperator": False,
                                       "overrides": {"enabled": True}})
            check("开启路线 + 无权重 + 未降级：显式失败（400/code=1）",
                  strict.status_code == 400 and strict.json()["code"] == 1,
                  json.dumps(strict.json(), ensure_ascii=False)[:200])

            # ---- 配置越界：阈值必须落在 0.20–0.40 ----------------------- #
            bad_score = client.post("%s/predict" % url,
                                    json={"uniqueId": "smoke_task_b", "isLastOperator": False,
                                          "overrides": {"enabled": True,
                                                        "min_clip_score": 0.9}})
            check("min_clip_score 越界被拒绝（400）",
                  bad_score.status_code == 400 and bad_score.json()["code"] == 1,
                  json.dumps(bad_score.json(), ensure_ascii=False)[:200])

            # ---- 非法输入 ------------------------------------------------ #
            bad_id = client.post("%s/uploadFile?uniqueId=..%%2Fescape" % url,
                                 files={"file": ("bundle.zip", bundle, "application/zip")})
            check("非法 uniqueId 上传被拒绝（422）",
                  bad_id.status_code == 422 and bad_id.json()["code"] == 1,
                  json.dumps(bad_id.json(), ensure_ascii=False)[:200])

            slip = io.BytesIO()
            with zipfile.ZipFile(slip, "w") as archive:
                archive.writestr("../escape.jsonl", "{}\n")
            slip_response = client.post("%s/uploadFile?uniqueId=smoke_slip" % url,
                                        files={"file": ("evil.zip", slip.getvalue(),
                                                        "application/zip")})
            check("zip 目录穿越被拒绝（422）",
                  slip_response.status_code == 422 and slip_response.json()["code"] == 1,
                  json.dumps(slip_response.json(), ensure_ascii=False)[:200])

            cross = client.post("%s/predict" % url,
                                json={"uniqueId": "smoke_task_b", "isLastOperator": False,
                                      "inputFile": str(BASE_DIR / "configs" / "config.yaml")})
            check("跨任务 inputFile 被拒绝（400）",
                  cross.status_code == 400 and cross.json()["code"] == 1,
                  json.dumps(cross.json(), ensure_ascii=False)[:200])

            bad_bool = client.post("%s/predict" % url,
                                   json={"uniqueId": "smoke_task_b",
                                         "isLastOperator": "false"})
            check("isLastOperator 非布尔被拒绝（422）",
                  bad_bool.status_code == 422 and bad_bool.json()["code"] == 1,
                  json.dumps(bad_bool.json(), ensure_ascii=False)[:200])

            missing = client.post("%s/predict" % url,
                                  json={"uniqueId": "smoke_not_uploaded",
                                        "isLastOperator": False})
            check("未上传就预测被拒绝（400）",
                  missing.status_code == 400 and missing.json()["code"] == 1,
                  json.dumps(missing.json(), ensure_ascii=False)[:200])

            # ---- /downloadFile 必须不存在 -------------------------------- #
            download = client.post("%s/downloadFile?uniqueId=smoke_task_a" % url)
            check("/downloadFile 未注册（404/code=1）",
                  download.status_code == 404 and download.json()["code"] == 1,
                  json.dumps(download.json(), ensure_ascii=False)[:200])

            openapi = client.get("%s/openapi.json" % url).json()
            check("OpenAPI 只暴露三接口 + /health",
                  set(openapi["paths"].keys()) == {"/uploadFile", "/predict",
                                                   "/removeFile", "/health"},
                  str(sorted(openapi["paths"].keys())))

            # ---- 清理幂等 ------------------------------------------------ #
            removed = client.post("%s/removeFile?uniqueId=smoke_task_a" % url).json()
            again = client.post("%s/removeFile?uniqueId=smoke_task_a" % url).json()
            check("removeFile 幂等（True → False）",
                  removed["data"]["removed"] is True and again["data"]["removed"] is False,
                  json.dumps([removed["data"], again["data"]], ensure_ascii=False))
            check("删除后任务目录确实不存在",
                  not (output_dir / "smoke_task_a").exists())
            check("另一任务未被误删", (output_dir / "smoke_task_b").is_dir())
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:      # pragma: no cover
            server.kill()

    failed = [name for name, ok, _detail in RESULTS if not ok]
    print("\n共 %d 项检查，通过 %d 项，失败 %d 项"
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    if failed:
        print("失败项：" + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
