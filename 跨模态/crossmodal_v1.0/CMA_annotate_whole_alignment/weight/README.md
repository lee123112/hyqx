# weight/ · 算子 #16「标注整体对齐关系」本地模型权重

本目录存放算子 #16 运行所需的本地模型权重。运行时**只从本地路径加载，不联网下载**；
权重/依赖不可用时按配置显式 `degraded` 或 `failed`，**绝不使用随机初始化权重或固定分数冒充模型分数**。

## 1. 文件与校验

| 文件 | 模型 | 架构 | 字节数 | SHA-256 |
|---|---|---|---:|---|
| `clip/ViT-B-32.pt` | CLIP-ViT/B-32 | `ViT-B-32`（open_clip 权重） | 353,976,522 | `40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af` |

- 来源：`G:\HYQX\算子\跨模态\crossmodal_package_20261026\weight\clip\ViT-B-32.pt`。
- 本目录副本与原文件逐字节一致（SHA-256 相同），复制后未做任何转换。

## 2. 默认加载路径

`utils/backends.py::model_root()` 的解析顺序：

1. `$MODEL_ROOT` 已设置 → 配置里的相对路径按 `$MODEL_ROOT` 解析，**覆盖优先**；
   覆盖目录中没有权重时显式报 `weights_not_found` / `degraded`，**不会偷偷回退**本算子 `weight/`；
2. 未设置 `$MODEL_ROOT` → 默认取**本算子目录** `weight/`，即：

```
<算子目录>/weight/clip/ViT-B-32.pt
```

配置项（`configs/config.yaml`，字段/值保持冻结）：

```yaml
model:
  model_name: ViT-B-32
  weights: clip/ViT-B-32.pt
```

部署方式二选一：

- **默认**：权重随算子目录一起拷贝/打包，相对路径直接命中本目录；
- **覆盖**：`-e MODEL_ROOT=/models`，把权重按 `/models/clip/ViT-B-32.pt` 挂载。

## 3. 目录结构

```
CMA_annotate_whole_alignment/
├── configs/config.yaml
├── utils/backends.py
└── weight/
    ├── README.md
    └── clip/
        └── ViT-B-32.pt
```

> 权重为大二进制文件，打包上传时可单独走对象存储/挂载盘，不建议压进 zip 分发包。
