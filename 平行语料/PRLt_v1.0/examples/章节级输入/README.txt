章节级输入使用说明
==================
共3组章节，每组有独立中文TXT和英文TXT：
- documents/gran-chapter-001：19段对19段。
- documents/gran-chapter-002：30段对30段。
- documents/gran-chapter-003：11段对11段。
合计60个输入段落对；这不是保证最终交付60条的质量结论。

工程界面测试：每次选同一document目录中的source_zh.txt和target_en.txt作为一对文件。首次推荐gran-chapter-001。每个自然段在一个非空行，中间保留空行。

正式API测试：
1. 将documents和plans目录完整准备到同一个服务器输入根目录，保留相对结构。
2. 上传input.jsonl（3篇）或最小样例.jsonl（仅001），或单文档清单/<document_id>.jsonl（指定1篇）。
3. 提交/predictChain，inputMode=documents，inputFile使用/uploadFile回包路径；planDirectory填写服务器上plans目录的绝对路径。参考提交示例.json，替换所有占位符。uniqueId为taskId_fileId形式，每次使用新值。
4. 完成后/downloadFile获取所需阶段ZIP；文档对齐阶段ID为08，默认整链末阶段ID为17。
5. /uploadFile单次只上传一个文件，不会自动解压ZIP或安排文档对。仅上传清单不能替代服务器上应有的计划和TXT。

分段整理：
001沿用2026-11-13已修正的19对段落；002把英文原非空段9–10合并成一段、29–45法规列表合并成一段，47段调整为30段；003原样保留。只合并英文分段/空白，所有文字、标点和数字顺序保留；中文TXT逐字节不变。来源all目录未被本次整理改动。
清单中的source/target全文与本目录TXT一致，计划元数据中的段落数同步更新。保留原计划fallback_policy=rule_degraded及原参数，不覆盖模型或算法阈值。

各TXT、JSON和JSONL均为UTF-8无BOM。不要将整个章节作为普通pairs长句CSV测试，以免与章节入口和长度过滤混淆。