"""算子 23「压缩交付对齐语料包」工具包。

本目录自带共享契约层（``io`` / ``schemas`` / ``lineage`` / ``transport`` / ``logger``），
业务规则在 ``delivery``，打包在 ``archive``。**不跨算子目录 import**：
每个算子独立启动、独立打包，保证镜像可单独搬运。
"""
__all__ = ["archive", "delivery", "io", "lineage", "logger", "schemas", "transport"]
