# weight/ · 算子 #20「质检对齐产物」本地模型权重

本目录存放算子 #20 图文反向检索所需的本地模型权重。运行时**只从本地路径加载，不联网下载**；
权重缺失时反向检索显式 `degraded`（`insufficient_evidence` + `model_unavailable`），
**不使用随机初始化权重产出任何相似度分数**。

## 1. 文件与校验

| 文件 | 模型 | 架构 | 字节数 | SHA-256 |
|---|---|---|---:|---|
| `clip/ViT-B-32.pt` | CLIP-ViT/B-32 | `ViT-B-32`（open_clip 权重） | 353,976,522 | `40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af` |

- 来源：`G:\HYQX\算子\跨模态\crossmodal_package_20261026\weight\clip\ViT-B-32.pt`。
- 本目录副本与原文件逐字节一致（SHA-256 相同），复制后未做任何转换。

## 2. 默认加载路径

`utils/reverse_verify.py::model_root()` 的解析顺序：

1. `$MODEL_ROOT` 已设置 → 配置里的相对路径按 `$MODEL_ROOT` 解析，**覆盖优先**；
   覆盖目录中没有权重时显式 `clip_weights_missing` / `degraded`，**不会偷偷回退**本算子 `weight/`；
2. 未设置 `$MODEL_ROOT` → 默认取**本算子目录** `weight/`，即：

```
<算子目录>/weight/clip/ViT-B-32.pt
```

配置项（`configs/config.yaml`，字段/值保持冻结）：

```yaml
reverse_verify:
  clip:
    library: auto
    model_name: ViT-B-32
    weights: clip/ViT-B-32.pt   # 默认加载本算子 weight；null=复用工程侧共享实例
```

部署方式二选一：

- **默认**：权重随算子目录一起拷贝/打包，相对路径直接命中本目录；
- **覆盖**：`-e MODEL_ROOT=/models`，把权重按 `/models/clip/ViT-B-32.pt` 挂载。

## 3. 目录结构

```
CMA_check_alignment_quality/
├── configs/config.yaml
├── utils/reverse_verify.py
└── weight/
    ├── README.md
    └── clip/
        └── ViT-B-32.pt
```

> 权重为大二进制文件，打包上传时可单独走对象存储/挂载盘，不建议压进 zip 分发包。
