"""算子 20「质检对齐产物」共享工具包。

模块划分：
- ``schemas``：部署配置、请求覆盖与 ``/predict`` 请求模型；
- ``io``：流式 JSONL、路径安全、任务锁、原子写、安全解压；
- ``transport``：``crossmodal-files-v1`` 文件内容对象与运行清单；
- ``lineage``：血缘记录、阶段上下文与阶段血缘视图；
- ``logger``：规范日志；
- ``media_carry``：媒体按原相对路径搬运；
- ``media_probe``：文件可打开 / 视频可解码 / 音频非全静音 / 文本非空；
- ``upstream_metrics``：PPL / BRISQUE / SNR 与反向验证结论的上游优先复用；
- ``reverse_verify``：图文 CLIP 反向检索；
- ``alignment_qc``：本算子核心业务规则与台账、报告、偏科清单。
"""
