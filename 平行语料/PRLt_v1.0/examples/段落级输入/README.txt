段落级输入使用说明
==================
共3条中英自然段，ID为gran-paragraph-001、002、003。
1. 首次测试：上传“最小样例.csv”，仅含gran-paragraph-001。
2. 批量测试：选择input.csv或input.jsonl，均含相同3条记录，二选一提交。
3. 单条测试：单条样例目录下每个CSV只有1个双语段落。
4. 使用pairs入口；每条记录的source/target各是一段完整原文。提交示例.json的inputFile替换为/uploadFile回包data.inputFile；uniqueId采用taskId_fileId形式，每轮换新值。
5. 正式调用为/uploadFile → /predictChain → /downloadFile。后续句子对齐阶段可将一个段落拆成多条句对，因此输入段落数与最终句对数分别核对。
6. CSV为UTF-8 BOM，JSONL为UTF-8无BOM。JSONL每个非空行是完整JSON对象；input.jsonl总共3行，不是JSON数组。
7. 原文、译文、业务ID保留，metadata附带详细来源/许可及原数据列。最终通过条数由原算子规则决定。