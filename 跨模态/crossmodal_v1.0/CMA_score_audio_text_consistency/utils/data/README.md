# 离线词典与字母表（随算子包离线交付）

本目录存放字符→拼音（无调）映射，供 `utils/pinyin.py` 在**完全离线**的部署环境解析。
算子运行期绝不下载任何词典或模型；本目录随算子包一起搬运即可。

| 文件 | 说明 |
| --- | --- |
| `TSCharacters.txt` | OpenCC `TSCharacters` 词典，UTF-8；4148 条 `繁体<TAB>简体候选…` |
| `PinyinTable.txt` | 字符→拼音（无调）表，UTF-8；26703 条 `字符<TAB>音节`，覆盖 CJK 扩展 A 与基本区 |
| `LICENSE-OpenCC.txt` | OpenCC 项目 Apache-2.0 许可证原文 |
| `LICENSE-pypinyin.txt` | pypinyin 项目 MIT 许可证原文（`PinyinTable.txt` 的生成来源） |

## 来源与校验

### TSCharacters.txt

- 上游地址：<https://github.com/BYVoid/OpenCC/blob/master/data/dictionary/TSCharacters.txt>
- 下载日期：2026-10-20
- 文件大小：104520 字节
- SHA256：`9FF46A7D30E5765375EB13D33F2B03A34D298913CAF2B120380679F33AE1642D`
- 许可：Apache-2.0（词典文件头自带声明，另见 `LICENSE-OpenCC.txt`）

### PinyinTable.txt

- 生成来源：pypinyin 0.55.0（MIT），`lazy_pinyin(char)[0]`，无调、单读音
- 生成日期：2026-11-05
- 覆盖范围：U+3400–U+9FFF 中的 26703 个可转写字符
- 文件大小：215733 字节
- SHA256：`97BD416313AE2ACA2DAE6EAF13D43D4B3F01B83952B1EBA2B092E05925256715`
- 许可：MIT（见 `LICENSE-pypinyin.txt`）
- 用途：`utils/pinyin.py` 计算「同音字替换代价为 0」的字符级编辑距离比例，识别
  Whisper 同音误写（如「张工程师→張宮城師」「午休时→胡修実」），避免把 ASR
  同音错误判成「语义不通」；阈值 `edit_distance_ratio_max` 与分母口径不变

## 解析顺序（`utils/pinyin.py`）

1. 本目录 `utils/data/PinyinTable.txt`（算子包自带，优先）；
2. `$MODEL_ROOT/pinyin/PinyinTable.txt`（部署目录覆盖）。

表缺失或条目数 < 1000 时同音容错不可用：`homophone_tolerance` 自动按关闭处理（回退纯字符
CER 口径，结果不会更宽松），每条评分结果的 `pinyin_dictionary_source` /
`pinyin_table_entries` 记录实际来源，可审计。

## 解析顺序（`utils/t2s.py`）

1. 本目录 `utils/data/TSCharacters.txt`（算子包自带，优先）；
2. `$MODEL_ROOT/opencc/TSCharacters.txt`（部署目录，例如 `G:\HYQX\算子\model\opencc`）；
3. `utils/t2s.py` 内置的高频兜底映射（638 对常用繁简字，仅用于全量词典缺失时兜底）。

文件不存在、编码错误或有效条目数 < 100 时会被跳过并继续下一来源。实际使用的来源与
条目数写入每条评分结果的 `t2s_dictionary_source` / `t2s_mapping_entries` 字段，可按运行
结果审计。需要关闭归一化时在 `configs/config.yaml` 设 `traditional_to_simplified: false`
（或 `/predict` 的 `config` 覆盖同名布尔项）。

## 更新方式

映射只做单字符逐字转换并取每行首个候选，不做词组消歧。升级词典时：重新生成/下载
上述文件 → 更新本 README 的大小、SHA256 与日期 → 同步复制到 `$MODEL_ROOT` 对应子目录。
