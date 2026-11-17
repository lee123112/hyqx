# 06 样本版本 · 算子说明书（OPERATOR_MANUAL）

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

## 一、基础信息

| 项 | 内容 |
|---|---|
| 算子名称 | 样本版本 |
| 算子英文 ID | `avp_pack_version` |
| 算子目录 | `AVP_VERSION_DATA_006` |
| 正式编号 | 6 |
| 版本 | v1.0.0 |
| 所属阶段 | S1 样本生产 · 阶段末（05 质检之后） |
| 上游输入 | 05 样本质检的 `005_batch_final.jsonl` + `005_qc_final.json` |
| 下游输出 | 07 评测（版本 ID / manifest）、10 资产统计（版本/样本数）、前端版本列表 |
| 运行形态 | FastAPI `/predict` 单算子调用；也支持 CLI |
| 依赖 | Python 3.9+、numpy、PyYAML；**无模型、无 GPU、无网络、不解析音频** |

---

## 二、功能定义

本算子把「质检通过的批次」变成「可交付的版本」，并管理版本生命周期：

### 2.1 `action=pack`：打包版本

1. 读取最终样本表，逐行校验 `qc.status`（必须是 `pass` / `rejected`，缺了说明 05 没跑 finalize）；
2. **只把 `pass` 样本打进版本**，`rejected` 排除并计数（`excluded_rejected`）；
3. 每条音频算 SHA-256 与字节数，写进 `manifest.json`；
4. 生成 `006_version.zip`：`manifest.json` + `005_batch_final.jsonl`（仅 pass）+ `audio/`；
5. **回读 zip 自检**每个样本的哈希与字节数，不通过则整次失败；
6. 版本号同任务内同名自增（v1.0 → v1.1），写注册表，产出 `006_version.json`。

版本名称长度按业务限制 2–50 字（`version.name_min_len` / `name_max_len`），空名或 1 个字直接
`invalid_params:`。

### 2.2 `action=publish`：发布版本

1. 定位版本（显式 `version_id`，否则取最新草稿）；
2. 状态必须是 `草稿`；质检结论必须是 `通过`（否则 `qc_not_passed:`）；
3. 版本包必须存在且 sha256 与注册表一致（防替换）；
4. 状态改 `已发布`，写 `published_at`，对外可见（`visible=true`）。

### 2.3 `action=rollback`：回滚版本

1. 版本必须存在且状态为 `已发布`；
2. 状态改 `已回滚`，写 `rolled_back_at`；
3. **只改状态**：不复制、不删除任何样本或版本包文件，包 sha256 不变；
4. 已回滚版本不能再发布、不能重复回滚。

### 2.4 `action=compare`：版本对比

1. 默认取最近两个 `已发布` 版本；也支持显式 `version_id` + `compare_with`；
2. 以 `sample_id` 做集合差：新增 / 删除 / 共有；
3. 共有样本再比 `sha256`：找出「ID 相同但音频被换过」的内容变化；
4. 输出 `006_compare.json`（计数 + 明细 + 截断说明），只读不改注册表。

### 2.5 明确不做的

- 不做质检/可听性判定（05 的职责）；
- 不做评测、归因、建议、报告（07–11 的职责）；
- 回滚不做文件恢复/删除；
- 不写数据库、不做对象存储、不做并发锁。

---

## 三、输出内容描述

| 文件 | 何时产出 | 说明 |
|---|---|---|
| `006_version.zip` | pack | 交付副本：manifest + 最终样本表 + 音频 |
| `006_version.json` | pack / publish / rollback | §8 契约 + manifest 明细 + 可见性 |
| `006_compare.json` | compare | 两版本差异 |
| `{work_dir}/006_registry.json` | 每次动作 | 版本注册表（07/10 的复用入口） |
| `{work_dir}/006_packages/<version_id>.zip` | pack | 权威版本包（每版一份，不覆盖） |

关键字段：

- `status`：`草稿` / `已发布` / `已回滚`；
- `visible`：是否对外可见（只有已发布为 true）；
- `manifest.samples[]`：每条 `{sample_id, audio_path, sha256, bytes}`；
- `manifest.verify`：打包自检结果（`checked/passed/rate/batch_in_zip/manifest_count_matches`）；
- `manifest.zip`：版本包 `{path,bytes,sha256}`，path 相对 `work_dir`；
- `statistics`：与接口返回完全一致。

---

## 四、技术方案

### 4.1 内容寻址：让「版本里有什么」可验证

版本的本质不是「一个文件夹」，而是「一组内容哈希」。本方案：

1. 对每条音频计算原始文件 SHA-256 与字节数，写入 `manifest.json`；
2. 包内音频用 `audio/<sample_id>.<ext>` 的相对路径；
3. 版本包整体再算一个 SHA-256，注册表保存；
4. 发布时校验包 sha256，防止「包被换了还按旧版本发布」。

这样任何一条音频被替换、截断、重复打包，都能在 manifest 层被发现，
而不是等到评测指标异常才反推。

### 4.2 打包后回读自检

写完 zip 立刻重新打开，逐条比对 `sha256` 与 `bytes`，并确认样本表在包内。
自检通过率就是指标 `manifest sha256 校验通过率`（实测 100%）。
不通过时整次失败（`internal_error:`），不产出「看起来成功」的坏包。

### 4.3 状态机而不是布尔标记

```
草稿 ──publish──► 已发布 ──rollback──► 已回滚
```

- 发布必须同时满足「草稿 + 质检通过 + 包哈希一致」，避免把待复核版本放出去；
- 回滚只改状态，文件原样保留，可审计、可对比；
- 已回滚不能重新发布，杜绝「先回滚再偷偷上架」。

### 4.4 注册表单文件 + 原子写

`006_registry.json` 是唯一状态源，用「临时文件 + rename」原子写。
单任务单进程，不需要数据库；07/10 直接读注册表即可拿到版本 ID、样本数、manifest，
不需要解包或重算哈希。

### 4.5 版本包持久化：权威副本 + 交付副本

- 每个版本在 `006_packages/<version_id>.zip` 留一份权威包，永不覆盖；
- 输出目录的 `006_version.zip` 是本次交付副本；
- 发布/回滚/对比只信注册表与权威包，避免同名版本互相覆盖。

### 4.6 与「纯复制目录」的区别

| 维度 | 复制目录 | 本算子 |
|---|---|---|
| 内容完整性 | 无校验 | 逐样本 sha256 + 回读自检 |
| 版本追溯 | 目录名 | 注册表：状态/时间/包哈希/manifest |
| 回滚 | 可能删文件 | 只改状态，文件不动 |
| 发布控制 | 无 | 质检结论 + 包哈希双校验 |
| 对比 | 人工 diff | 集合差 + 内容哈希差异 |

---

## 五、技术实现

| 文件 | 职责 |
|---|---|
| `processor.py` | 参数校验、批次读取、manifest 构建、zip 写入/自检、注册表读写、四个动作 |
| `configs/config.yaml` | 文件名/版本号规则/默认结论/截断条数等全部配置化 |
| `utils/__init__.py` | 工具包占位 |
| `tests/` | processor 层 31 例 + 接口层 4 例 |
| `dataset/` | 字段契约 + 3 条可打包样例 |
| `output/model/` | 真实指标与配置快照 |

实现要点：

- **确定性**：同一输入、同一版本名 → 版本号稳定自增；manifest 按输入行顺序生成；
- **失败不吞**：缺 `qc`、重复 `sample_id`、音频缺失、包自检失败都明确报错；
- **不改上游**：只读 `005_batch_final.jsonl` 与音频，不移动/重命名源文件；
- **原子写**：注册表与版本 JSON 都走 `write_json` 的临时文件 + rename；
- **日志**：route/read/progress/result/write_output，单行 ≤160 字，不泄露容器绝对路径。

---

## 六、资源要求

| 项 | 要求 |
|---|---|
| CPU | 单核即可；打包瓶颈是磁盘 IO 与 SHA-256 |
| 内存 | 60 条约 37 MB（含解释器）；建议容器 ≥512 MB |
| 磁盘 | 每个版本 ≈ 音频原始体积（zip 对 PCM16 压缩有限）+ 少量 JSON；最新版本多一份交付副本 |
| GPU | 不需要 |
| 网络 | 不需要 |
| 系统依赖 | 无 libsndfile/ffmpeg 依赖（06 不解析音频） |

---

## 七、性能指标

实测环境：Windows，Python 3.9.7，单进程顺序执行，12 逻辑核。

| 指标 | 实测值 |
|---|---|
| manifest sha256 校验通过率 | **100%**（v1.0 15/15，v1.1 15/15） |
| 剔除样本不进版本 | 输入 18 → 打包 15，排除 3 |
| 版本号序列 | v1.0 → v1.1 |
| 版本对比 | 样本数差 0；新增 1、删除 1、内容变化 1；共有 14、完全相同 13 |
| 回滚文件数 | **6 → 6**（文件列表一致） |
| 回滚后包 sha256 | **不变** |
| 60 条打包耗时 | 均值 **293.4 ms**（最小 278.5 / 最大 317.7 ms），4.89 ms/条 |
| 处理完成后 RSS | 36.7 MB（基准 36.1 MB） |
| 版本包体积（60 条 1s/16k） | 1,166,801 bytes（约 1.1 MB） |
| 单测 | 35 passed（31 processor + 4 api） |

> 数据集为程序化合成，数字证明实现正确性，不代表真实业务语料的压缩率与分布。

---

## 八、输入输出示例

### 8.1 pack 请求

```json
{
  "operator": "AVP_VERSION_DATA_006",
  "uniqueId": "ver006_demo",
  "params": {"action": "pack", "version_name": "客服噪声集-v1"}
}
```

（未传 `qc_conclusion` 时回读同目录 `005_qc_final.json`；样例结论为「通过」。）

### 8.2 pack 响应（真实冒烟，节选）

```json
{
  "code": 0,
  "data": {
    "status": "success",
    "statistics": {
      "input_samples": 3, "packed_samples": 3, "excluded_rejected": 0,
      "version_no": "v1.0", "manifest_verify_rate": 1.0, "zip_bytes": 178931
    },
    "outputs": {
      "version_zip": "/data/ver006_demo/tmp/006/006_version.zip",
      "version": "/data/ver006_demo/tmp/006/006_version.json"
    },
    "result": {
      "action": "pack", "version_id": "VER-20261102141600-001",
      "version_no": "v1.0", "sample_count": 3,
      "qc_conclusion": "通过", "status": "草稿"
    }
  }
}
```

### 8.3 publish 响应（节选）

```json
{
  "code": 0,
  "data": {
    "result": {
      "action": "publish",
      "version_id": "VER-20261102141600-001",
      "version_no": "v1.0",
      "status": "已发布",
      "published_at": "2026-11-02T14:16:10+08:00",
      "sample_count": 3
    }
  }
}
```

### 8.4 rollback 响应（节选）

```json
{
  "code": 0,
  "data": {
    "result": {
      "action": "rollback",
      "version_id": "VER-20261102141600-001",
      "version_no": "v1.0",
      "status": "已回滚",
      "rolled_back_at": "2026-11-02T14:16:26+08:00",
      "sample_count": 3
    }
  }
}
```

### 8.5 compare 响应（节选）

```json
{
  "code": 0,
  "data": {
    "result": {
      "action": "compare",
      "version_a": "VER-20261102141600-001",
      "version_b": "VER-20261102141612-001",
      "sample_count_diff": 0,
      "added_count": 0,
      "removed_count": 0,
      "sha256_diff_count": 0
    },
    "outputs": {"compare": "/data/ver006_demo/tmp/006/006_compare.json"}
  }
}
```

### 8.6 失败示例

```json
{"code": 1, "msg": "qc_not_passed: 版本 v1.0 质检结论为「待复核」，未通过质检不允许发布"}
```

```json
{"code": 1, "msg": "invalid_params: 版本 v1.0 当前状态为「已回滚」，只允许从「草稿」发布"}
```

```json
{"code": 1, "msg": "version_not_found: 可比较的「已发布」版本不足 2 个（当前 0 个）"}
```
