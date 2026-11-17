# 优化建议报告 · 甲方算子说明书

> 部署包说明（2026-11-12）：本文保留原业务说明；开发测试、示例数据和历史指标文件在原 AVP 源包中，本部署版本仅包含运行文件，实际布局见同目录 README。

## 一、基础信息

| 项 | 内容 |
|---|---|
| 算子名称 | 优化建议报告 |
| 算子编号 | 11（`AVP_REPORT_DATA_011`） |
| 英文标识 | `avp_generate_report` |
| 所属阶段 | S5 报告中心（阶段末算子） |
| 标签 | 报告生成 |
| 版本 | v1.0.0 |
| 交付形态 | 单算子目录 + `run(ctx)` 入口，由 `inference.py` 统一挂载 |
| 输入 | 007 评测结果（JSONL）+ 008 差异归因（JSON）+ 009 优化建议（JSON） |
| 输出 | `011_report.md` / `011_report.html` / `011_report_meta.json` |
| 依赖 | Python 3.9+ 标准库 + 包内 `common`；不接大模型、不联网、不依赖 LibreOffice |

## 二、功能定义

**解决的问题**：把评测、归因、建议三类上游结果汇编成一份**固定八段**、数字可追溯、可直接交付的「优化建议报告」。

**报告结构（固定八段，顺序不可改）**

1. 结论摘要——整体指标、主要差距、原因判断、优化建议、使用边界；
2. 整体指标——CER/WER/SER 与对比对象差值、样本量、异常样本、完成率；
3. 分场景表现——按 `dimensions[]` 的维度/取值列出样本数、占比、三项指标、与基准差值；
4. 噪声场景下钻——只保留噪声维度，按差值给出「差距最大/差距明显/差距较小」判断；
5. 错误形态——替换/删除/插入/未识别条数与占比、集中子类；
6. 原因分析——主因、占比、影响条数、置信度、归因因素表、归因链路；
7. 优化建议——按优先级列建议标题与事实/证据强度/解释/验证方式/补样计划；
8. 风险与限制——输入接入、样本规模、过程异常、对比口径、归因边界、口径边界、导出边界。

**触发时机**：评测→归因→建议链路完成后由报告中心调用；`isLastOperator=true` 时服务层会内联 Markdown 内容。

**不负责**

- 不重新评测、不重新归因、不生成新建议（只汇编上游结果）；
- 不接大模型写文案；PDF 由本算子用 reportlab 直出（不调 LibreOffice、不引外部中文字体文件）；
- 不导出样本明细，不落库。

## 三、输出内容描述

| 产物 | 内容 | 使用方 |
|---|---|---|
| `011_report.md` | Markdown 八段报告（含元信息、表格、提示块） | 前端预览、归档、二次编辑 |
| `011_report.html` | 自包含 HTML（内联 CSS、A4 打印样式、CSS 条形图），离线可看 | 报告中心网页预览 |
| `011_report.pdf` | **PDF 八段报告**（A4、页脚印报告编号与页码；`STSong-Light` CID 字体，不依赖外部字体文件） | 报告中心「下载报告」，可直接交付/打印 |
| `011_report_meta.json` | 报告编号/标题/生成时间/章节/输入接入/关键数字/告警/口径 | 编排层、审计、前端列表 |

**三种格式同源**：md / html / pdf 都由同一份内部 `model` 渲染，编号、数字、章节一字不差；`params.format` 取 `md`/`html`/`pdf`/`both`(md+html)/`all`(三者全出，默认)。
**PDF 降级**：环境里没有 reportlab 时只告警「PDF 未生成」，md/html 照常产出，`statistics.pdf_generated=false`。

**Markdown/HTML 八段对应关系**：`## 1. 结论摘要` … `## 8. 风险与限制`；HTML 使用 `<h2>` 同序。
**空值口径**：缺输入 → 「未接入：……」；数值缺失 → 「—」；不补零、不猜测。
**报告编号**：默认 `RPT-BENCH-{YYYYMMDD}-{NNN}`（序号从 001 起），可由 `params.report_no` 覆盖。

## 四、技术方案

1. **异构输入识别（文件名 + 内容双通道）**
   文件名匹配 `007_eval*` / `*attribution*` / `*advice*`；不匹配时按内容特征识别
   （`suggestions` / `main_cause` / `metrics`）；还支持「一个 JSON 三份输入」的 bundle 形态。
   识别结果写入 `sources[]`，保证报告数字能回溯到具体文件。

2. **单位口径自动换算（容错不猜测）**
   `metric_unit: auto`：三项指标全部 ≤1 → 按比例换算为百分数；否则按百分数直用。
   `ratio_unit` / `share_unit` 同理。这样上游口径在 0.076 与 7.6 两种写法间切换时报告不会出现 100 倍错误；
   也允许通过配置显式固定口径，避免 auto 的边界误判。

3. **结构化中间表示（IR）+ 双渲染器**
   章节先构造成结构化块（`rows/table/list/note/subhead/chips/unavailable`），再分别渲染 Markdown 与 HTML。
   两个渲染器共享同一 IR，保证两种格式内容一致；HTML 使用内联 CSS（`@page` A4、`@media print`、CSS 变量），
   无任何外部资源引用，满足内网离线导出要求。

4. **数字可追溯**
   报告不引入输入之外的数字：百分数来自输入或输入的直接换算（占比/差值/错误形态占比/异常占比/完成率）；
   关键值同时写入 `meta.key_numbers`。自验对齐全样本的 60 个百分数逐一回溯、15 个关键值逐项核对，均 100% 一致。

5. **确定性渲染**
   渲染是纯函数：同一结构化模型 + 同一配置 → 逐字节相同输出；只有 `generated_at` 与报告编号日期取系统时间。

6. **为什么不用大模型/模板引擎**
   业务要求「数字由系统算、文字用固定模板回填」（规范 §11 第 10 条）。
   大模型会引入不可控改写与幻觉风险，模板引擎会引入额外依赖；本实现用纯 Python f-string 组装 +
   结构化 IR，既满足固定模板，又保证数字零漂移、零外部依赖。

**相较「纯阈值拼接」的优势**：阈值只决定「差距明显/较小」等解释性标签与告警色，不参与数字生成；
报告事实（指标、样本量、归因占比）始终等于输入，阈值调整不会改变数字。

## 五、技术实现

| 文件 | 职责 |
|---|---|
| `processor.py` | 参数校验、输入识别、口径换算、八段装配、Markdown/HTML/PDF 渲染调度、meta 写出 |
| `avp_report_pdf.py` | PDF 渲染：reportlab + `STSong-Light` CID 字体，按文件路径懒加载（服务方式加载时算子目录不在 `sys.path`） |
| `configs/config.yaml` | 章节、阈值、单位口径、编号规则、展示上限等全部可调项 |
| `dataset/DATA_SCHEMA.md` | 输入字段来源/别名、输出字段、空值口径、错误码 |
| `dataset/sample/` | 三份小样本（007 JSONL / 008 JSON / 009 JSON） |
| `tests/` | pytest：正常/边界/异常/确定性/接口五类 |
| `output/model/` | `config.json` + `metrics.json`（真实指标） |

**处理流程**

```
校验 params(format/report_no/title/include_sections)
  → 定位输入（目录递归 / 文件同目录 / bundle）
  → 识别 007 / 008 / 009（文件名 → 内容嗅探）
  → 选取评测记录（latest）、换算单位、提取基准
  → 装配八段 IR（缺输入生成「未接入」块）
  → 渲染 Markdown / HTML / PDF（按 format；PDF 与另两种同源）
  → 写 011_report_meta.json（编号/标题/输入接入/关键数字/告警）
  → 日志（路由/读取/输出/告警/partial/完成）
```

**关键实现点**

- `_load_artifacts()`：目录扫描、bundle 拆分、重复输入告警；
- `_classify()`：文件名模式 + 内容嗅探；`_pick_eval()`：多条取最新；
- `_metric_block()` / `_ratio_block()` / `_share_percent()`：单位自动换算，避免 100 倍错误；
- `_section_*()`：八个章节各自独立、可单测；IR 块类型固定，渲染器只负责序列化；
- `avp_report_pdf.render()`：同一份 IR → platypus 流式排版（表格自动分页、页脚带报告编号与页码）；
- `_build_report_meta()`：报告自证信息（数字快照 + 输入溯源 + 告警）；
- 业务失败抛 `OpError`（`missing_file:` / `invalid_params:` / `invalid_jsonl:`），由服务层统一收口。

## 六、资源要求

| 项 | 要求 / 实测 |
|---|---|
| CPU | 1 核即可；纯字符串/JSON 处理 + PDF 排版 |
| 内存 | 进程 RSS 35.2 MB（含依赖导入） |
| 磁盘 | 单次产物约 28 KB（MD 3 KB + HTML 14 KB + PDF 11 KB + meta 3 KB） |
| 网络 | 不需要 |
| 依赖 | `reportlab`（仅 PDF 用；缺失时降级为不出 PDF 并告警），无 LibreOffice、无外部字体文件 |

## 七、性能指标

| 指标 | 结果 | 口径 |
|---|---|---|
| 单次 `run()` 延迟 | **25.38 ms**（min 24.41 / max 27.50） | 三份输入齐全，预热 1 次后 5 次均值 |
| 八段齐全率 | **3/3 = 100%** | 齐全 / 仅 007 / 仅 009 三种组合 |
| 百分数可追溯率 | **60/60 = 100%** | 齐全组合 Markdown 全部百分数 |
| 关键数字一致率 | **15/15 = 100%** | 指标/样本量/异常量/主因占比/错误形态/建议数等 |
| HTML 自包含 | **3/3** | 无外链 CSS/JS/CDN |
| 确定性 | 同模型两次渲染逐字节一致 | 纯函数渲染 |

复现命令与完整输出见 `DEVELOPMENT_STATUS.md`。

## 八、输入输出示例

**输入**：`dataset/sample/` 三份文件（007 一条评测、008 一份归因、009 一条 P0 建议），参数 `{"format": "all"}`。

**响应摘要（真实运行输出）**

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "status": "success",
    "statistics": {"sections": 8, "suggestions": 1, "eval_results": 1,
                   "has_attribution": true, "has_advice": true,
                   "pdf_generated": true,
                   "missing_inputs": [], "warnings": 0, "inputs": 3},
    "result": {
      "report_no": "RPT-BENCH-20261102-001",
      "title": "客服场景回归 · 优化建议报告",
      "format": "all",
      "conclusion": "success",
      "missing_inputs": []
    },
    "outputs": {
      "report_md": "/data/cli_smoke_011/tmp/011/011_report.md",
      "report_html": "/data/cli_smoke_011/tmp/011/011_report.html",
      "report_pdf": "/data/cli_smoke_011/tmp/011/011_report.pdf",
      "report_meta": "/data/cli_smoke_011/tmp/011/011_report_meta.json"
    }
  }
}
```

**PDF 实测**（真实数据 · 18 条样本的八段报告）：A4 **3 页**、**10,962 字节**、页脚印 `RPT-BENCH-20261103-001` 与「第 N 页」；用 `pypdf` 抽取文本可正常取到中文与数字，与同名 Markdown 一致。

**报告节选（Markdown）**

```markdown
# 客服场景回归 · 优化建议报告

- **报告编号**：RPT-BENCH-20261102-001
- **样本量**：1,200 条
- **异常样本**：36 条（3.0%）
- **对比口径**：豆包语音（第三方）

## 1. 结论摘要

**整体结论**：九天语音 v2.3 整体 CER 7.6%、WER 12.1%、SER 17.8%，评测样本 1,200 条；对比豆包语音（第三方）的差值 +0.8 / +1.6 / +1.9 pp。

**主要差距**：主因为 交通噪声识别差距，占整体退化 62.0%，影响 9 条，整体净多错 9 条。

## 2. 整体指标

| 指标 | 本模型 | 对比对象 | 差值（pp） |
| --- | --- | --- | --- |
| CER | 7.6% | 6.8% | +0.8 |
| WER | 12.1% | 10.5% | +1.6 |
| SER | 17.8% | 15.9% | +1.9 |
```

**缺输入示例**：只给 `007_eval_results.jsonl` 时 `status=partial`，`missing_inputs=["008 差异归因","009 优化建议"]`，
报告仍为八段，原因分析与优化建议写「未接入」，绝不补数字。

**失败示例**：

```json
{"code": 1, "msg": "invalid_params: format 取值 'pdf' 非法，允许：md / html / both"}
```
