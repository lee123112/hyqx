# -*- coding: utf-8 -*-
"""语音加扰产线（语音对抗平台）算子包 · 共享底层能力。

本包只放**跨算子复用的底层能力**，不放任何业务规则：

- :mod:`common.audio`    音频解码 / 重采样 / 单声道 / PCM16 写出 / 时长 / RMS / 按 SNR 混音 / SNR 估计 / 变速
- :mod:`common.iofs`     文件与清单读写（JSONL / JSON / zip 安全解压 / 清单解析 / uttid 归一化）
- :mod:`common.logger`   按《日志规范模板》产出前端「处理过程」可用的日志行
- :mod:`common.lineage`  MTY 血缘记录构造与跨阶段拼接
- :mod:`common.schemas`  `{code,msg,data}` 外壳、错误码、入参校验
- :mod:`common.config`   `configs/config.yaml` 加载（环境变量 ``CONFIG_PATH`` 优先）

注意：``common.io`` 与 Python 内置 ``io`` 重名会造成导入陷阱，因此文件名为 ``iofs.py``。
"""

SERVICE_NAME = "audio-adversarial-pipeline"
SERVICE_TITLE = "语音加扰（语音对抗平台）算子包"
PACKAGE_VERSION = "v1.0.0"

__all__ = [
    "SERVICE_NAME",
    "SERVICE_TITLE",
    "PACKAGE_VERSION",
]
