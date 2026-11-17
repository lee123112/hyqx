# M19 门禁兼容修复（2026-11-09）

读取 v4.2 `process_status`，兼容旧字符串 `status` 和结论 `decision`，覆盖评分、整体对齐、时序、局部标注及 M17/M18/M21 隔离。M11 拍平时非空深合并，空媒体或空处理节点不遮住真实证据，且不改入参对象。

M13/M14 全局开启但行无对应模态时，`no_audio_text_pair` / `no_video_text_pair` 不算本行生效路线。是否有媒体同时检查直接引用、`*_reference`、声明模态、根级路径和 M17 typed/generic 路径。已有媒体但缺描述、转写或模型仍为未决，不能以不适用状态豁免。至少仍需一条真正适用的路线通过；缺模态本身不是通过证据。显式 `passed=false` 不被状态文字或不适用理由覆盖。

保留已批准优化：M13 `language_not_applicable` / `transcript_unreliable`，M14 `temporal_route_not_applicable` 可由其他真实路线继续判定。M19 默认 `temporal_route_fallback=true` 保留：仅当 #12/#13/#14 都未生效，且 #15 适用并确实通过，才令 `enabled_routes=["15"]`。关闭配置仍待处理；已有评分路线时不介入；失败或不适用时序不能充当路线通过。

四门和授权条件不变。`whole=not_applicable` 保持待处理；整体对齐仍必须取得真实自动/人工对齐结论。M17/M18/M21 已隔离样本不加回。时序显式失败优先，其他路线通过不能覆盖。

验证：新增 44 条规则回归；完整目录 `python -X utf8 -m pytest tests -q -ra` 为 **220 passed / 30 skipped**；30 项均为已停用的 v4.2 血缘/运输测试。现有 8 条时序路线兜底测试原样通过；无模型权重加载、无阈值变更。
