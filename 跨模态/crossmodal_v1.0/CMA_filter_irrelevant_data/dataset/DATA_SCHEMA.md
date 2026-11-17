# 算子 2 数据 Schema

入口为 UTF-8 JSONL，每行必须是对象并包含唯一 `sample_id`。`text` 为内联文本，`image`、`video`、`audio` 为相对素材包根目录的路径。主输出 `02_screened.jsonl` 保留完整样本并附 `prescreen` 证据；`02_rejects.jsonl` 保存行号、原因和必要证据。
