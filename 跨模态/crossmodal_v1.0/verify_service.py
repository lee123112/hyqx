"""跨模态算子包 v1.0.0 · 部署验收脚本（仅标准库）

用法：
    python verify_service.py                          # 默认 127.0.0.1:8100（容器内）
    python verify_service.py --port 17059             # 直连宿主机端口
    python verify_service.py --no-preflight           # 跳过注册表/权重预检（宿主无依赖时用）

校验项：
    1. GET /health        code=0 且 operatorCount=26
    2. GET /ready         code=0
    3. GET /operators     200、code=0 且列出 26 个算子
    4. 内置权重文件与 SHA256SUMS.txt 一致（18 个文件）
    5. ffmpeg / ffprobe 可用（#6 关键帧、#7 音频标准化依赖）
    6. python inference.py --preflight 通过（operatorCount=okCount=26，weightBundles 无缺失）
    7. 运行环境不得出现模型网关/远程模型变量（本包模型全部本地加载）

退出码：0=全部通过；1=存在失败项。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent
GATEWAY_ENV_TOKENS = ("MODEL_GATEWAY", "MODEL_APP_ID", "MODEL_APP_KEY", "APPKEY", "MODEL_PATH", "OPENAI")

results = []


def record(ok: bool, name: str, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(("PASS  " if ok else "FAIL  ") + name + ((" | " + detail) if detail else ""))
    return ok


def info(name: str, detail: str = "") -> None:
    print("INFO  " + name + ((" | " + detail) if detail else ""))


def request(base: str, path: str, timeout: float = 10.0):
    """返回 (status, payload)，保留 HTTP 错误响应供验收输出。"""
    req = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return exc.code, {}


def check_endpoints(base: str) -> None:
    try:
        status, payload = request(base, "/health")
        data = payload.get("data") or {}
        record(status == 200 and int(payload.get("code", -1)) == 0,
               "GET /health 返回 code=0",
               "http=%s code=%s version=%s operatorCount=%s"
               % (status, payload.get("code"), data.get("version"), data.get("operatorCount")))
        record(int(data.get("operatorCount", 0)) == 26, "池化注册表算子数=26",
               "operatorCount=%s" % data.get("operatorCount"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        record(False, "GET /health 可达", "%s: %s" % (type(exc).__name__, exc))
        return

    try:
        status, payload = request(base, "/ready")
        data = payload.get("data") or {}
        record(status == 200 and int(payload.get("code", -1)) == 0, "GET /ready 返回 code=0",
               "state=%s readyCount=%s" % (data.get("status"), data.get("readyCount")))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        record(False, "GET /ready 可达", "%s: %s" % (type(exc).__name__, exc))

    try:
        status, payload = request(base, "/operators")
        count = len((payload.get("data") or {}).get("operators") or [])
        record(status == 200 and int(payload.get("code", -1)) == 0 and count == 26,
               "GET /operators 返回 200 且列出 26 个算子",
               "http=%s code=%s operators=%d" % (status, payload.get("code"), count))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        record(False, "GET /operators 可达", "%s: %s" % (type(exc).__name__, exc))

def check_weights() -> None:
    sums = PKG_ROOT / "SHA256SUMS.txt"
    if not sums.is_file():
        record(False, "SHA256SUMS.txt 存在", str(sums))
        return
    entries = []
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            entries.append((parts[0].lower(), parts[1].strip()))
    record(len(entries) == 18, "SHA256SUMS.txt 条目数=18", "entries=%d" % len(entries))
    bad = []
    total = 0
    for expected, rel in entries:
        path = PKG_ROOT / rel
        if not path.is_file():
            bad.append("%s 缺失" % rel)
            continue
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        total += path.stat().st_size
        if h.hexdigest() != expected:
            bad.append("%s 哈希不一致" % rel)
    record(not bad, "18 个内置权重文件 SHA-256 校验",
           "合计 %.1f MB%s" % (total / 1048576.0, "" if not bad else "；" + "; ".join(bad[:5])))


def check_system_bins() -> None:
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(name)
        record(path is not None, "系统依赖 %s 可用" % name, path or "未找到")


def check_preflight() -> None:
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", "-B", "inference.py", "--preflight"],
                              cwd=str(PKG_ROOT), capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        record(False, "inference.py --preflight 可执行", "%s: %s" % (type(exc).__name__, exc))
        return
    if proc.returncode != 0 and not proc.stdout.strip():
        record(False, "inference.py --preflight 可执行", (proc.stderr or "").strip()[:300])
        return
    # 取第一个以 { 开头的行作为 JSON 起点，兼容预检前的日志输出。
    lines = (proc.stdout or "").splitlines()
    start = next((idx for idx, line in enumerate(lines) if line.strip().startswith("{")), None)
    payload = "\n".join(lines[start:]) if start is not None else ""
    try:
        report = json.loads(payload)
    except ValueError:
        record(False, "inference.py --preflight 输出可解析",
               (proc.stdout or proc.stderr).strip()[:300])
        return
    bundles = report.get("weightBundles") or {}
    record(report.get("operatorCount") == 26 and report.get("okCount") == 26,
           "preflight operatorCount=okCount=26",
           "operatorCount=%s okCount=%s" % (report.get("operatorCount"), report.get("okCount")))
    record(not bundles.get("missing"), "preflight 内置权重无缺失",
           "checked=%s ok=%s" % (bundles.get("checked"), bundles.get("ok")))

    runtime = report.get("runtime") or {}
    if runtime:
        info("运行设备",
             "machine=%s torch=%s cuda=%s npu=%s threads=%s"
             % (runtime.get("machine"), runtime.get("torch"), runtime.get("cudaAvailable"),
                runtime.get("torchNpuInstalled"), runtime.get("torchThreads")))
        info("设备解析", str(runtime.get("deviceMode") or ""))
        expect_cpu = os.getenv("EXPECT_CPU", "").strip().lower() in {"1", "true", "yes", "on"}
        if expect_cpu:
            cpu_ok = (not runtime.get("cudaAvailable")) and \
                str(runtime.get("deviceMode") or "").startswith("cpu")
            record(cpu_ok, "运行设备为纯 CPU（EXPECT_CPU=1；8 个模型算子走 CPU）",
                   "cudaAvailable=%s mode=%s" % (runtime.get("cudaAvailable"), runtime.get("deviceMode")))


def check_no_gateway_env() -> None:
    hits = []
    for key, value in os.environ.items():
        upper = key.upper()
        if upper == "MODEL_ROOT":
            continue
        if upper.startswith("MODEL_") or upper in {"APPKEY", "APP_KEY", "API_KEY", "OPENAI_API_KEY"} \
                or any(tok in upper for tok in GATEWAY_ENV_TOKENS):
            hits.append("%s=%s" % (key, value))
    record(not hits, "无模型网关/远程模型环境变量（模型全部本地加载）", "; ".join(hits[:5]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="跨模态算子包部署验收")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--no-preflight", action="store_true")
    args = parser.parse_args(argv)

    base = "http://%s:%d" % (args.host, args.port)
    print("== 跨模态算子包 v1.0.0 验收（%s）==" % base)
    print("\n-- 服务接口 --")
    check_endpoints(base)
    print("\n-- 内置模型权重 --")
    check_weights()
    print("\n-- 系统依赖 --")
    check_system_bins()
    if not args.no_preflight:
        print("\n-- 注册表/目录预检 --")
        check_preflight()
    print("\n-- 模型调用方式 --")
    check_no_gateway_env()

    failed = [item for item in results if not item[0]]
    print("\n== 汇总：%d 项通过，%d 项失败 ==" % (len(results) - len(failed), len(failed)))
    for _, name, detail in failed:
        print("  FAIL %s %s" % (name, detail))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
