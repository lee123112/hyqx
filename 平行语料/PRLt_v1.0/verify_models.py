#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正式版模型权重完整性一键校验脚本（PRLt_V1.0 · 2026-10-31）。

校验对象（当前正式版模型，见 ``models/NEW_MODELS_PROVENANCE.json``）：
  - Qwen3-Embedding-0.6B   → API09 句子级对齐 / API11 领域识别 / API14 语义等价
  - mdeberta-v3-base       → API10 子句/短语级对齐
  - Qwen3.5-2B             → API15 译文流畅度 / API22 句式变换
  - 硅基流动 Qwen3.5-9B    → API20 回译 / API23 跨语言合成（远程 API，不落盘权重）

判定规则：
  [OK]    正式版必需目录/文件存在，且 SHA256 与 ``NEW_MODELS_PROVENANCE.json`` 实测值一致
          （--no-hash 模式下“文件存在”即 OK）。
  [LOCAL] 开发侧生成的适配器/原型文件（``models/LOCAL_RECONSTRUCTION.json`` 登记）：
          存在且 SHA256 与登记值一致 -> 放行；缺失/不符只告警，不判失败。
  [LEGACY] 旧版模型目录（LaBSE/XLM-R/BGE-M3/Qwen2.5/NLLB）：仅提示，不参与判定。
  [WARN]  选项/环境提示（如未设置 SILICONFLOW_API_KEY）。
  [FAIL]  仅正式版必需项缺失或 SHA256 不符 -> 计入失败。

用法：
  python verify_models.py              完整校验（含 SHA256，约 7GB，1~3 分钟）
  python verify_models.py --no-hash    只校验目录与文件名（秒级）

退出码：0 = 正式版必需项全部通过；1 = 存在必需项缺失或 SHA256 不符。
"""

import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(ROOT, "models")
PROVENANCE_PATH = os.path.join(MODELS, "NEW_MODELS_PROVENANCE.json")
LOCAL_MARKER = os.path.join(MODELS, "LOCAL_RECONSTRUCTION.json")

LEGACY_DIRS = [
    ("models/labse", "旧 API09 LaBSE"),
    ("models/xlm-roberta-base", "旧 API10 XLM-R"),
    ("models/bge-m3-safetensors", "旧 API14 BGE-M3"),
    ("models/bge-m3", "旧 API14 BGE-M3 备份"),
    ("models/Qwen2.5-1.5B-Instruct", "旧 API15/22 Qwen2.5"),
    ("models/nllb-200-distilled-600M", "旧 API20/23 NLLB"),
    ("models/bge-domain-prototype", "旧 API11 领域分类器"),
]

OPERATOR_LABEL = {
    "Qwen3-Embedding-0.6B": "API09/11/14 Qwen3-Embedding-0.6B",
    "mdeberta-v3-base": "API10 子句/短语 mDeBERTa-v3-base",
    "Qwen3.5-2B": "API15/22 Qwen3.5-2B（本地）",
}


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def main():
    no_hash = "--no-hash" in sys.argv
    provenance = load_json(PROVENANCE_PATH) or {}
    models = provenance.get("models") or []
    local = (load_json(LOCAL_MARKER) or {}).get("files") or {}

    print("=" * 66)
    print("正式版模型权重完整性校验（PRLt_V1.0）")
    print("项目根目录:", ROOT)
    print("校验模式:", "仅存在性" if no_hash else "完整(SHA256)")
    print("=" * 66)

    failures = 0
    warnings = 0
    ok_count = 0
    local_count = 0

    if not models:
        print("[FAIL] 无法读取 models/NEW_MODELS_PROVENANCE.json 或清单为空")
        sys.exit(1)

    # 1. 正式版必需目录
    print("\n[1] 正式版模型目录")
    for model in models:
        rel = "models/" + model["local_dir"]
        label = OPERATOR_LABEL.get(model["local_dir"], model["local_dir"])
        exists = os.path.isdir(os.path.join(ROOT, rel))
        print(f"    {'[OK]  ' if exists else '[FAIL]'} {rel:40s} # {label}")
        if not exists:
            failures += 1

    # 2. 正式版必需文件 + SHA256
    print("\n[2] 正式版必需文件 + SHA256")
    for model in models:
        for filename, meta in (model.get("required_files") or {}).items():
            rel = f"models/{model['local_dir']}/{filename}"
            path = os.path.join(ROOT, rel)
            expect = str(meta.get("sha256") or "").strip().lower()
            if not os.path.isfile(path):
                print(f"    [FAIL] {rel:55s} -> 文件缺失")
                failures += 1
                continue
            if no_hash or not expect:
                print(f"    [OK]   {rel:55s} (存在, 未验 SHA)")
                ok_count += 1
                continue
            got = sha256_of(path)
            if got == expect:
                print(f"    [OK]   {rel:55s}")
                ok_count += 1
            else:
                print(f"    [FAIL] {rel:55s} -> SHA 不匹配")
                print(f"           期望 {expect}")
                print(f"           实际 {got}")
                failures += 1

    # 3. 开发侧适配器/原型件（本地重建，登记 SHA；缺失/不符仅告警）
    print("\n[3] 开发侧适配器/原型件 ([LOCAL]，缺失/不符仅告警)")
    if local:
        for rel, entry in local.items():
            local_count += 1
            expect = str((entry or {}).get("sha256") or "").strip().lower()
            reason = (entry or {}).get("reason") or "本地生成件"
            path = os.path.join(ROOT, rel)
            if not os.path.isfile(path):
                print(f"    [LOCAL]{rel:55s} # {reason}")
                print("           警告: 文件缺失（不判失败；正式交付前需补齐）")
                warnings += 1
                continue
            if no_hash or not expect:
                print(f"    [LOCAL]{rel:55s} # {reason} (存在, 未验 SHA)")
                continue
            got = sha256_of(path)
            if got == expect:
                print(f"    [LOCAL]{rel:55s} # {reason} (SHA 与登记一致)")
            else:
                print(f"    [LOCAL]{rel:55s} # {reason} -> 警告: SHA 与登记值不一致")
                print(f"           期望 {expect}")
                print(f"           实际 {got}")
                warnings += 1
    else:
        print("    [WARN] 未找到 models/LOCAL_RECONSTRUCTION.json 或清单为空")

    # 4. 旧版模型目录（仅提示）
    print("\n[4] 旧版模型目录（已退役，仅提示）")
    for rel, label in LEGACY_DIRS:
        exists = os.path.isdir(os.path.join(ROOT, rel))
        print(f"    {'[LEGACY]' if exists else '[----]  '} {rel:40s} # {label}")

    # 5. 远程 API 提示
    print("\n[5] 远程 API 模型（API20/API23）")
    key_set = bool(os.getenv("SILICONFLOW_API_KEY", "").strip())
    print(f"    SILICONFLOW_API_KEY = {'已设置' if key_set else '未设置（调用时会结构化失败，不伪造译文）'}")
    if not key_set:
        warnings += 1

    # 6. 汇总
    print("\n" + "=" * 66)
    print("汇总: 正式版 [OK] %d 项 | 本地件 [LOCAL] %d 项 | 告警 %d 项 | 失败 %d 项"
          % (ok_count, local_count, warnings, failures))
    if failures == 0:
        print("结果: 正式版模型全部就绪。")
        if warnings:
            print("注意: 存在 %d 项告警（本地件缺失/不符或未配置 API Key），请逐条确认。" % warnings)
    else:
        print("结果: 存在 %d 项失败，请补齐缺失文件或核对 SHA。" % failures)
    print("=" * 66)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
