# 算子说明书：文本清洗标准化与分词（text_normalization）

| 项 | 内容 |
|---|---|
| 算子名称 | 文本清洗标准化与分词 |
| demo ID | `text_normalization` |
| 版本 | v1.0.0 |
| 所属阶段 | S2（跨模态预处理阶段），编排默认 `isLastOperator=false` |
| 服务入口 | `uvicorn app:app --host 0.0.0.0 --port 8103` |
| 镜像入口 | `packaging/Dockerfile` → `CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8103"]` |
| 技术栈 | Python 3.10+、FastAPI/Uvicorn/Pydantic、PyYAML、标准库 `unicodedata`/`re` |
| 训练需求 | **无需训练**，无模型权重，不联网下载 |
| 正式平台编号 | 待甲方对照表，`map.json` 中 `formal_operator_numbers` 为 `null`，不编造 |

> **表1 口径**：本算子只做兜底清洗（乱码/控制字符、空文本、标点统一），**不分词**；名称中的“分词”不在本算子实现范围，上游已有分词结果原样沿用。

## 1. 用途

对上游 `02_screened.jsonl` 的文本做三项保守兜底清洗：删除替换字符（U+FFFD）等乱码、删除控制/代理字符（保留制表/换行/回车）、把中文全角标点统一为半角 ASCII（如 `，`→`,`、`。`→`.`、`“”`→`"`）；清洗后为空的文本记为 `empty_text_after_cleaning` 并拒绝。**不分词、不去重、不改大小写、不解析 HTML、不删广告文本、不合并空白**；不新增 `tokens`/`token_offsets`。同时把记录引用的媒体搬到本次运行目录、保留全部字段，向下游传递完整样本。

能处理：JSONL 内联字符串或对象文本、图文/音视频/纯文本记录、混合中英文与 emoji。
不处理：语言识别（`lang` 为声明值）、分词/去重、大小写转换、HTML 解析、广告/水印清洗、语义改写、翻译、质检判定、媒体解码校验（后续 04–06/21 负责）。

## 2. 输入输出

- 输入：完整记录 JSONL，一行一条；`text` 可为字符串或对象（取 `content`，回退 `original`）；媒体为相对路径。
- 输出：`03_text_cleaned.jsonl`（入口）、`03_rejects.jsonl`（旁路）、`manifest.json`（`crossmodal-manifest-v1`）、`run.log`（本地控制文件）。
- 运行目录：`<OUTPUT_DIR>/<uniqueId>/text_normalization/runNNN/`，目录内媒体位于 `media/`，整体可搬运。
- 字段级细节见 `dataset/DATA_SCHEMA.md`。

## 3. 接口

| 接口 | 请求 | 成功返回 |
|---|---|---|
| `POST /predict` | JSON：`uniqueId`、`isLastOperator`(严格 bool)、可选 `inputFile`、可选 `config`；或 multipart：`request`(JSON 字符串)+`file`(zip)；形态一为不带 `request` 的 multipart（`?detection=M04`，`file` 必传） | `data={"operator","status","outputFile","result"}`；形态一为 `{"status",...}` 判定 JSON |
| `GET/POST /downloadFile` | `uniqueId`(可选) | 不实现，固定 404 `not_implemented` |
| `POST /removeFile` | JSON `{"uniqueId":"…"}` | 删除任务目录；不存在时 `deleted=false`/`success=false` |

- [v4.2] `isLastOperator` 仍必传，用于标记阶段末调用；中间与阶段末响应同构，均返回 `data.outputFile`（`crossmodal-files-v1` 文件内容对象，UTF-8 文本 + base64 媒体，含入口、旁路清单、全部媒体与 `manifest.json`）+ `data.result`（`process_status`/`text`/`encoding`/`statistics`）；
- 不再返回 `lineageFile`，运行目录也不再写 `lineage.json` / `stage_context.json`；
- 失败统一为 `code=1` 信封，`success=false`，不携带成功产物；`data.error` 给出原因码。

## 4. 关键配置

`configs/config.yaml`。业务参数仅总开关 `enabled`（默认 `false`）可经请求 `config` 或 CLI `--override` 覆盖；`operator`、`stage_id`、`lang`、`io`、`server` 为部署固定项，请求不可覆盖。配置不接受分词、HTML、大小写、空白合并、广告正则等参数（写入即配置失败）。完整表见 `README.md` §4。

## 5. 部署

裸机：

```bash
pip install -r requirements.txt
python dataset/sample/make_sample.py          # 可选：生成自检样例
uvicorn app:app --host 0.0.0.0 --port 8103
# 环境变量：OUTPUT_DIR=<共享输出根>（默认 <算子目录>/output）
```

容器（`packaging/` 下执行）：

```bash
python prepare_package.py
docker build -t crossmodal-text-normalization:v1.0.0 .
docker run --rm -p 8103:8103 -v /host/output:/data/output \
  -e OUTPUT_DIR=/data/output crossmodal-text-normalization:v1.0.0
```

同阶段服务须挂载同一 `OUTPUT_DIR` 且容器内路径一致；本 demo 未实际执行 Docker 构建（见 `HANDOFF.md` 未验证项）。

## 6. 资源与性能

- 纯 CPU 规则文本处理，单进程即可；无 GPU 需求；
- 内存与单次响应体（`server.max_response_bytes`，demo 64MiB）及素材包上限（64MiB）相关，建议容器 ≥2GiB；
- 吞吐取决于行数与文本长度，无采样率/尺寸类阈值；实测统计见 `HANDOFF.md`。

## 7. 评价指标

本算子无模型，因此不报告精度/召回。验收以**确定性与守恒**为准：

- 清洗计数可复算：`chars_in`/`chars_out`、`control_chars_removed`、`replacement_chars`、`punctuation_normalized` 与输出文本一致；
- 清洗只做三项：开启时仅删除乱码/控制字符并统一标点，不改变大小写、词序与空白结构；
- `total_lines == valid_records + bad_lines`，`output + rejected == valid_records`，`conservation_ok=true`；
- 媒体搬运：被引用的媒体在运行目录内存在且字节与源一致；
- 语义保留：开启清洗时保留 `C++`、`10°C`、邮箱、emoji、负号与单位（标点映射除外）。

## 8. 降级与失败

| 情况 | 行为 |
|---|---|
| 请求 `config` 含 `enabled` 以外的业务键（如 `tokenizer`、`strip_html`、`lowercase_latin`、`ad_patterns`） | 配置校验失败，任务 `failed`（不静默改用另一种算法，不留指向不存在产物的成功记录） |
| 输入非 UTF-8 | `invalid_encoding` 失败，不猜其他编码 |
| 坏 JSON 行/非对象记录 | 计入 `bad_lines` 并进异常清单，不静默消失 |
| 媒体缺失/0 字节/路径逃逸 | 该记录进 `03_rejects.jsonl`（`media_missing:<字段>` 等），引用置 `null` |
| 清洗后文本为空 | 图文记录进拒绝清单（`empty_text_after_cleaning`）；video+audio 无文本为 `not_applicable` 保留 |

## 9. 已知限制

- 语言检测不在本算子范围内，`lang` 仅回显声明值；
- 不做分词/去重/大小写转换，也不解析 HTML；上游已有的 `tokens`/`token_offsets` 等字段原样沿用，本算子不新增；
- 标点统一是单向映射（中文全角→半角 ASCII），不做反向转换；
- U+FFFD 只计数后删除，不还原原字（无法凭空恢复）；
- 空白结构不改动（不合并连续空白、不去首尾），与“统一标点”是两件事；
- 响应为 demo 内联承载协议（`crossmodal-files-v1` + 64MiB 限额，[v4.2] 中间/阶段末同构），正式二进制协议到位时只替换 `utils/transport.py` 适配层。
