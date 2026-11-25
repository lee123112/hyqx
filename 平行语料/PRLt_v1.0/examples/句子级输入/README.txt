句子级输入使用说明
==================
共3条中英句对，原始业务ID保留。
1. 首次测试：上传“最小样例.csv”，只有1条干净句对（flores200-devtest-0049-s-0005）。
2. 批量测试：选择input.csv或input.jsonl，均含相同3条记录，二选一提交。
3. 单条测试：单条样例目录下每个CSV只有1条记录。
4. 使用pairs入口；提交示例.json中的inputFile替换为/uploadFile回包data.inputFile。uniqueId采用taskId_fileId形式，每轮测试换新值。然后/predictChain，最后/downloadFile。
5. 首条flores200-devtest-0005-s-0002为故意带噪的清洗/脱敏样例，包含HTML、页眉页脚、U+0001/U+0007控制符及测试联系方式；这些不是文件损坏，已按原始数据保留。
6. CSV包含带引号的嵌入换行，按CSV记录解析，不按文件物理行数计数。CSV为UTF-8 BOM，JSONL为UTF-8无BOM。
7. input.jsonl的metadata保留数据来源与授权说明；input.csv提供核心输入字段及常用来源字段。
8. 验收重点为可上传、输入记录数、阶段状态、清洗/脱敏/去留依据与下载产物；最终通过条数由原算子规则决定。