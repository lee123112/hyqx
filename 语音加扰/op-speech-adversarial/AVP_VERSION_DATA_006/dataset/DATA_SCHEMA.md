# 06 样本版本 · 数据契约（DATA_SCHEMA）

> 算子：`avp_pack_version`　目录：`AVP_VERSION_DATA_006`
> 上游：05 样本质检（`005_batch_final.jsonl` + `005_qc_final.json`）
> 下游：07 评测结果接收（版本 ID / manifest）、10 数据资产统计（版本与样本数）、前端版本列表
> 内部持久化：`{work_dir}/006_registry.json`

---

## 1. 输入

### 1.1 输入来源

| 动作 | 输入 | 说明 |
|---|---|---|
| `pack` | 最终样本表 JSONL（单个文件或目录） | 目录时按 `io.batch_filename_candidates` 找 `005_batch_final.jsonl` → `004_batch.jsonl` → `003_batch.jsonl` |
| `publish` / `rollback` / `compare` | 版本注册表 `006_registry.json` | 不需要批次表；publish 会校验版本包仍在且 sha256 未变 |

### 1.2 `005_batch_final.jsonl` 行字段（§8 契约）

| 字段 | 类型 | 必填 | 含义 | 空值口径 |
|---|---|---|---|---|
| `sample_id` | string | ✅ | 样本唯一 ID | 不空；重复 → `invalid_jsonl:` |
| `source_uttid` | string | ✅ | 来源 uttid | 不空 |
| `dataset_id` | string | ✅ | 数据集 ID | 不空 |
| `audio_path` | string | ✅ | 音频路径（绝对路径或相对工作/输入目录） | 不空；不存在 → `missing_file:` |
| `text` | string | ✅ | 参考文本 | 不空 |
| `duration` | float | ✅ | 时长（秒，3 位小数，沿用 05 快照） | 不空 |
| `perturb` | object | ✅ | 加扰快照（`dimension/value/snr_db/speed/asset`） | 不空 |
| `qc` | object | ✅ | 质检快照，**恰好 4 键** | 不空 |

`qc` 子字段：

| 键 | 类型 | 含义 | 空值口径 |
|---|---|---|---|
| `qc.status` | string | `pass` / `rejected` | 其他值 → `invalid_jsonl:` |
| `qc.snr_db` | float\|null | 自动检查 SNR | 允许 null |
| `qc.snr_source` | string\|null | `measured` / `estimated` | 允许 null |
| `qc.reason` | string | 判定依据 | 不空 |

### 1.3 `005_qc_final.json`（可选）

`pack` 未显式传 `qc_conclusion` 时，按「批次表同目录 → 上级目录 → work/tmp/005 → 输入目录」
回读该文件，取 `qc_conclusion` 或 `conclusion`；都没有则用 `version.default_qc_conclusion`（默认 `待复核`）。

### 1.4 参数（`params`）

| 参数 | 适用动作 | 类型 | 默认 | 含义 |
|---|---|---|---|---|
| `action` | 全部 | string | 无（必填） | `pack` / `publish` / `rollback` / `compare` |
| `version_name` | pack | string | 无（必填） | 版本名称，2–50 字；同任务内同名版本号自增 |
| `version_no` | pack | string\|null | null | 显式版本号，须匹配 `^v\d+\.\d+$`；不传则 v1.0 → v1.1 |
| `source_task` | pack | string\|null | null | 来源任务（可选，≤100 字） |
| `qc_conclusion` | pack / publish | string | 见 1.3 | `通过` / `待复核` / `不通过` |
| `version_id` | publish / rollback / compare | string | rollback 必填 | 版本 ID（`VER-<YYYYMMDDHHMMSS>-<seq>`） |
| `compare_with` | compare | string\|null | null | 基线版本 ID |
| `published_at` | publish | string\|null | 当前时间 | 发布时间（补录场景） |

---

## 2. 输出

### 2.1 `006_version.zip`（版本包，权威副本在 `{work_dir}/006_packages/<version_id>.zip`）

```
006_version.zip
├── manifest.json            # 版本 manifest（sha256 清单）
├── 005_batch_final.jsonl    # 仅 pass 行（rejected 不进入版本）
└── audio/
    ├── <sample_id>.wav      # 文件名经安全化处理；冲突时追加 _02 序号
    └── ...
```

`manifest.json` 结构：

| 字段 | 类型 | 含义 |
|---|---|---|
| `version_id` | string | 版本 ID |
| `version_name` | string | 版本名称 |
| `version_no` | string | 版本号 |
| `sample_count` | int | 包内样本数（= pass 条数） |
| `source_task` | string\|null | 来源任务 |
| `qc_conclusion` | string | 质检结论 |
| `status` | string | 打包时为 `草稿` |
| `created_at` | string | 创建时间 |
| `samples` | array | 每项 `{sample_id, audio_path, sha256, bytes}` |

> `audio_path` 是 **zip 内相对路径**（`audio/xxx.wav`）；`bytes` 是原始音频字节数；
> `sha256` 是原始音频文件的 SHA-256，用于回读校验与版本对比。

### 2.2 `006_version.json`（§8 契约 + 可追溯扩展）

| 字段 | 类型 | 含义 | 空值口径 |
|---|---|---|---|
| `version_id` | string | 版本 ID | 不空 |
| `version_name` | string | 版本名称 | 不空 |
| `version_no` | string | 版本号 | 不空 |
| `sample_count` | int | 版本样本数 | ≥0 |
| `source_task` | string\|null | 来源任务 | 允许 null |
| `qc_conclusion` | string | `通过` / `待复核` / `不通过` | 不空 |
| `status` | string | `草稿` / `已发布` / `已回滚` | 不空 |
| `published_at` | string\|null | 发布时间 | 未发布为 null |
| `manifest` | object | `{file, sample_count, zip:{path,bytes,sha256}, verify:{...}, samples:[...]}` | 不空 |
| `created_at` | string | 创建时间（扩展字段） | 不空 |
| `rolled_back_at` | string\|null | 回滚时间（扩展字段） | 未回滚为 null |
| `visible` | bool | 是否对外可见（= `status=="已发布"`） | 不空 |

> 扩展字段只增不改名；§8 要求的 8 个字段全部保留。

### 2.3 `006_compare.json`

| 字段 | 类型 | 含义 |
|---|---|---|
| `compare_at` | string | 对比时间 |
| `version_a` | object | 基线版本摘要（`version_id/name/no/sample_count/status/created_at/published_at`） |
| `version_b` | object | 目标版本摘要 |
| `sample_count_diff` | int | `b.sample_count - a.sample_count` |
| `added_count` / `removed_count` | int | 目标新增 / 删除的 sample_id 数 |
| `sha256_diff_count` | int | 两边都有但音频内容不同的条数 |
| `common_count` / `identical_count` | int | 共有条数 / 其中完全相同条数 |
| `added_samples` / `removed_samples` / `changed_samples` | string[] | 明细，受 `io.max_listed` 截断 |
| `truncated` | object | `{added, removed, changed}` 各列表被截断的条数 |
| `note` | string | 口径说明 |

### 2.4 `006_registry.json`（内部持久化，供 07/10 复用）

| 字段 | 类型 | 含义 |
|---|---|---|
| `task_id` | string | 当前 `uniqueId` |
| `updated_at` | string | 最后更新时间 |
| `versions` | array | 版本记录，每项含 `version_id/.../zip/manifest` |

版本记录字段：`version_id`、`version_name`、`version_no`、`sample_count`、`source_task`、
`qc_conclusion`、`status`、`created_at`、`published_at`、`rolled_back_at`、
`zip:{path,bytes,sha256}`、`manifest:{file,samples,verify}`。

> `zip.path` 相对 `work_dir`（如 `006_packages/VER-...zip`），不写容器绝对路径。

### 2.5 `statistics` 字段

| 动作 | 键 |
|---|---|
| `pack` | `input_samples`、`packed_samples`、`excluded_rejected`、`version_no`、`manifest_samples`、`manifest_verify_checked`、`manifest_verify_passed`、`manifest_verify_rate`、`zip_bytes` |
| `publish` / `rollback` | `version_id`、`version_no`、`sample_count`、`status` |
| `compare` | `baseline_version`、`target_version`、`baseline_samples`、`target_samples`、`sample_count_diff`、`added`、`removed`、`sha256_diff` |

---

## 3. 状态机与业务判定

### 3.1 版本状态机

```
pack ──► 草稿 ──publish（qc=通过 + 包 sha 校验）──► 已发布 ──rollback──► 已回滚
           │                                            │
           └── 不可直接回滚                              └── 不可再回到已发布 / 草稿
```

| 当前状态 | 允许动作 | 结果 |
|---|---|---|
| `草稿` | publish | `已发布` |
| `草稿` | rollback | ❌ `invalid_params:` |
| `已发布` | publish | ❌ `invalid_params:` |
| `已发布` | rollback | `已回滚` |
| `已回滚` | publish / rollback | ❌ `invalid_params:` |

### 3.2 `pack` 判定

| 情形 | 处置 |
|---|---|
| 任一行缺 `qc.status` 或值非法 | `invalid_jsonl:`（必须先跑 05 finalize） |
| `qc.status=rejected` | **不进入版本**；`excluded_rejected` 计数 |
| 无任何 `pass` 行 | `invalid_jsonl:` |
| `sample_id` 重复 | `invalid_jsonl:` |
| 音频不存在 | `missing_file:` |
| 打包后回读校验不是 100% | `internal_error:`（不静默产出坏包） |
| 版本名重复 | 同任务内版本号自增（v1.0 → v1.1） |

### 3.3 `publish` / `rollback` 硬口径

- **只有「已发布」对外可见**（`visible=true`）；
- publish 前校验：状态必须是草稿、质检结论必须是 `通过`、版本包必须存在且 sha256 与注册表一致；
- **回滚只改状态**：不复制、不删除任何样本/版本包文件，版本包 sha256 不变；
- 回滚后版本保留在注册表里（可审计、可对比），只是不再对外可见。

### 3.4 `compare`

- 默认取最近两个「已发布」版本（按 `published_at` → `created_at` → `version_id` 排序）；
- 显式 `version_id` + `compare_with` 时以参数为准；只给一个时自动补另一个已发布版本；
- 对比对象不能是同一个版本；
- 只读，不改注册表、不动版本包。

---

## 4. 时间、哈希、空值口径

| 项 | 口径 |
|---|---|
| 时间 | ISO8601 带时区（`2026-11-02T14:16:00+08:00`） |
| 时长 | 秒，float，沿用 05 的 3 位小数 |
| `sha256` | 小写十六进制 64 位；音频算原始文件，版本包算整个 zip |
| `bytes` | int，原始字节数 |
| `rate` | float，4 位小数（`1.0` 表示 100%） |
| 空值 | 统一 `null`；未发布 `published_at=null`，未回滚 `rolled_back_at=null` |
| 编码 | UTF-8 无 BOM；JSON 缩进 2 空格，JSONL 单行一对象 |

---

## 5. 错误码

| 错误码 | 触发 |
|---|---|
| `missing_file:` | pack 无输入/音频不存在；publish 版本包丢失 |
| `invalid_jsonl:` | 坏 JSONL、缺 `qc`、非法 `qc.status`、重复 `sample_id`、无 pass 行、注册表损坏 |
| `invalid_params:` | action/version_name/version_no/version_id 非法；状态不允许该动作；包 sha 与注册表不一致 |
| `version_not_found:` | publish/rollback/compare 指定的版本不存在；可比较的已发布版本不足 2 个 |
| `qc_not_passed:` | publish 时质检结论不是「通过」 |
| `internal_error:` | 打包回读校验未达 100%、未预期异常 |

> 业务拦截（`qc_not_passed` / 状态非法）与系统错误都走 `code=1`，`msg` 前缀区分，前端按前缀提示。

---

## 6. 样例目录

```
dataset/sample/
├── 005_batch_final.jsonl    3 条 pass 样本（含 qc 快照，可直接 pack）
├── 005_qc_final.json        质检结论「通过」（供 pack 回读）
└── audio/
    ├── clean.wav
    ├── mix_20db.wav
    ├── mix_22db.wav
    └── mix_blind_25db.wav
```

样本由 `tools/fixture_lib.py` 程序化合成，**不是真实业务录音**；合计约 258 KB。
