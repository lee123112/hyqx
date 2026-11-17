# 算子说明书 · 22 生成跨模态Token

> 面向平台配置员与业务人员。技术细节见 [`README.md`](README.md) 与 [`dataset/DATA_SCHEMA.md`](dataset/DATA_SCHEMA.md)。
> 当前版本：`v1.0.1`（2026-10-30 补充单调用输出定位字段，配置口径不变）。

## 一、这个工具做什么

把**已经通过质检、且不在隔离区**的对齐样本，写成两类可交付文件：

1. **样本文件**（`samples.jsonl`，文件名固定）：一条样本一行，字段完整、可追溯；
2. **Token 文件**（`22_tokens.jsonl`）：把文本里的模态位置写成标记，例如
   `一只橘猫趴在绿色的草地上` + 图像 → `<image> 一只橘猫趴在绿色的草地上`。

同时产出**元数据索引**（模态、对齐、质量、采集时间、版本、文件访问链接），
供后续检索与数据打包使用。

## 二、配置项（对应表1 模板）

| 表1 配置项 | 页面控件 | 本工具取值与行为 |
|---|---|---|
| 总开关 | 开关 | **默认开，不能关**（置“关”会直接报配置错误） |
| 1. 文字里用什么标记模态 | 只读 | 图像 `<image>`；音频 `<audio>`；视频 `<video>`。按样本实际模态填写；**没打开的路线不会生成对应文件** |
| 2. 样本文件名 | 只读 | 固定 `samples.jsonl`。**隔离区里的样本不会写进这个文件** |
| 路线开关 | — | 图文（默认开，不可关）、音文、视文、局部标注（默认关，可开） |

本工具还有两个运维参数（非表1 模板项）：

- `missing_quality_policy`：`waiting_manual`（默认，留产线）或 `failed`（严格模式，用于投产前口径校验）；
- `transport.max_response_bytes`：单次接口响应上限，默认 64 MiB。

## 三、处理规则（“标签 + 动作 + 去向”）

| 输入情况 | 标签 | 动作 | 去向 |
|---|---|---|---|
| 没有质量结论 | 流程-待质量结论 | 等待人工 | 留产线（`22_pending.jsonl`），**不生成 Token、不隔离** |
| 授权结论缺失/无法判定 | 流程-待补授权 | 等待人工 | 留产线 |
| 缺对齐文本 | 流程-待补文本 | 等待人工 | 留产线 |
| 适用路线未开启（如音文路线关着却有音文样本） | 流程-路线未开启 | 等待人工 | 留产线 |
| 授权结论=不可训练 | 样本-不可训练（明细标注「样本-敏感」） | 单独列出 | `22_untrainable.jsonl`（不进样本文件，建议去向：样本隔离库） |
| 已被上游显式锁定（重跑超限等） | 样本-已隔离 | 跳过 | `22_isolated_skipped.jsonl` |
| 已在隔离区（#21/#20 隔离、#17 不完整、#18 隔离） | 样本-已隔离 | 跳过 | `22_isolated_skipped.jsonl`（**不进样本文件**） |
| 质检结论为 flagged（#20 旁路） | 样本-样本构建：已生成Token | 保留 | 正常生成，另列 `22_flagged.jsonl` 供复核 |
| 通过全部检查 | 样本-样本构建：已生成Token | 保留 | `samples.jsonl` + 对应路线文件 |
| 缺 sample_id/pair_id、媒体路径非法 | 原始文件-缺字段 / 原始文件-路径非法 | 原始隔离 | `22_rejects.jsonl` |
| 坏行、非 JSON 对象、非 UTF-8 编码 | 原始文件-无法解析 / 原始文件-编码异常 | 拒绝 | `22_rejects.jsonl` |

**顺序即优先级**：原始文件问题 > 隔离 > 没有质量结论 > 授权 > 文本 > 路线 > 生成。

## 四、完成标准（可直接核对）

1. 默认图文任务能在 `samples.jsonl` 中写出以 `<image>` 开头的 Token；
2. 隔离区样本条数在 `samples.jsonl` 中为 **0**（都在 `22_isolated_skipped.jsonl`）；
3. 未开启的音文 / 视文 / 局部标注**没有对应文件**（`samples_audio_text.jsonl` 等不存在）；
4. `statistics.conservation_ok = true`（输入=有效+坏行；有效=生成+待处理+不可训练+隔离跳过+原始隔离）；
5. 本工具**不压缩、不改版本状态**：`compressed=false`、`version_state_changed=false`，`version` 仅为快照回显；
6. 原文件**只存链接**：输出目录不含任何媒体副本（无 `media/`），每条链接登记 `object_key`。

## 五、使用步骤

1. 上传输入包：`POST /uploadFile?uniqueId=<任务标识>`（zip，含入口 JSONL 与可选 `manifest.json`）；形态一也可直接把输入 ZIP（例如 #21 的 `outputFile.zip`）作为 `file` 提交；
2. 执行：`POST /predict`（形态一 `multipart/form-data` 传 `file`，返回 `status`、`uniqueId`、`runDir`、`outputFile`、`token_records`、`samples_jsonl`、`manifest`、`sample_count`、`isolated_skipped_count`；形态二 JSON 传 `uniqueId`/`isLastOperator`），阶段内后续算子传上一响应的 `result.outputFile`；
   - `isLastOperator` 仍必传（严格布尔），仅标记阶段末调用；v4.2 起**中间与阶段末响应同构**，均返回 `outputFile`（`crossmodal-files-v1` 文件内容对象）+ `result`（业务摘要），不再返回 `lineageFile`；
3. 取产物：从 `data.outputFile` 文件内容对象解码即得 `samples.jsonl` / `22_tokens.jsonl` / 各清单；
4. 清理：`POST /removeFile?uniqueId=<任务标识>`（只清理本任务临时目录，幂等）。

命令行等价用法：

```powershell
python processor.py --input dataset/sample/demo.jsonl --unique-id smoke_cli --output-root output_smoke
```

## 六、常见问题

**Q1：为什么运行成功但没有 Token？**
A：样本还没有质量结论（#20 未出结论）。此时 `status=degraded`、`statistics.pending_records>0`，
样本留在产线等待，不隔离也不生成——完全按表1「还没有质量结论：不拦，留产线中，不生成Token」。

**Q2：为什么音文样本明明有，却没有音文文件？**
A：音文路线默认关闭。把它打开（配置或请求 `overrides`）后才会产出 `samples_audio_text.jsonl`。

**Q3：`subword_token_ids` 一直是 null？**
A：本算子模型列为 `--`，没有子词分词器。Token 指**模态标记封装**，不伪造分词结果；
如需真实 token id，应由带分词器的下游环节补齐。

**Q4：媒体文件去哪了？**
A：按 FGW 口径「原始图像、视频等数据文件存储在对象存储中，数据库中仅存文件的访问链接」，
本算子**只登记链接**（`object_key` + `reference_only`），不搬运、不复制媒体。

**Q5：可以关掉总开关吗？**
A：不可以。表1 明确「默认开。不能关。」，配置成 false 会直接失败，避免静默跳过关键环节。
