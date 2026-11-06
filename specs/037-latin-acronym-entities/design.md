---
phase: design
---

# 037 设计：第三条抽取通道 + 停用词兜底

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 任务：[tasks.md](tasks.md)

## 1. 根因

| # | 位置 | 问题 |
|---|---|---|
| 1 | `back_end/src/memory/services/labeler.py:1046-1110`（`_extract_with_jieba`） | 只对 `nr`/`ns`/`nt` 调 `_add`；`eng` token 仅作为「中文名词 + ≤2 位编号」的合并候选，独立出现时不处理 |
| 2 | `back_end/src/memory/services/labeler.py:149-151`（`_CODED_PATTERN`） | 要求 `项目|系统|产品|平台|版本` 前缀/后缀，`ESB` 不匹配 |
| 3 | `back_end/src/memory/services/labeler.py:1215-1240`（`_extract_entities`） | 只有 `_CODED_PATTERN` + `_extract_by_suffix`（中文后缀扫描）两条通道 |
| 4 | `_extract_with_rules`（jieba 不可用时的降级） | 同样只扫中文 |

结论：不是「ESB 被判成噪声」，而是**根本没有一条规则会看它**。

## 2. 修复方案：加第三条通道，不动前两条

```
_ACRONYM = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_\-]{1,9}(?![A-Za-z0-9_])")

_STOP_ACRONYMS = frozenset({
    "API","SQL","JSON","HTTP","HTTPS","URL","URI","ID","OK","CPU","RAM","IO",
    "UI","UX","DB","GET","POST","PUT","DELETE","JS","TS","CSS","HTML","XML",
    "YAML","PDF","EXCEL","PPT","AND","OR","NOT","THE","FOR","ALL","NEW","TODO",
})

def _looks_like_acronym(word: str) -> bool:
    if not 2 <= len(word) <= 10:      return False
    if word.upper() in _STOP_ACRONYMS: return False
    if word.islower():                return False   # 纯小写噪声太大
    if word.isdigit():                return False
    uppers = sum(1 for c in word if c.isupper())
    return uppers >= 2 or any(c.isdigit() for c in word) or (word.isupper() and len(word) <= 6)
```

接入点（`_extract_entities` 内部，按顺序）：

```
1. _CODED_PATTERN.finditer(summary)   → 先占位（项目A 整体已收）
2. _extract_by_suffix(summary)        → 中文后缀实体
3. _ACRONYM.finditer(summary)         → 跳过与 1 重叠的区间，再 _add(kind="topic")
```

**为什么顺序重要**：`项目A` 会同时被 `_CODED_PATTERN` 与 `_ACRONYM` 匹配。
先跑 `_CODED_PATTERN` 并用 `seen` 去重不够 —— 还会剩下孤立的 `A`；
所以第 3 步要**跳过第 1 步已覆盖的字符区间**，而不是只按名字去重。

## 3. 判据细节

| 输入 | 结果 | 理由 |
|---|---|---|
| `ESB` | topic | 全大写、长度 3 |
| `PyCharm` | topic | 2 个连续大写 |
| `Kafka` | topic | 2 个连续大写（K + …）→ 实际只有首字母大写 |
| `MQ` | topic | 全大写、长度 2 |
| `项目A` | topic（一条） | `_CODED_PATTERN` 先占位，第 3 步跳过 |
| `API` | 空 | 停用词 |
| `and` | 空 | 纯小写 |
| `esb` | 空 | 纯小写（口语小写化的漏收，见风险） |

注意 `Kafka`：只有首字母大写，按上面 `uppers >= 2` 判据会被漏掉。所以判据里补一条
「首字母大写 + 后面 ≥2 个小写字母 + 总长 ≥4」→ 覆盖 CamelCase 单词。最终判据：

```
uppers >= 2
or any(c.isdigit() for c in word)
or (word.isupper() and len(word) <= 6)
or (word[0].isupper() and len(word) >= 4 and word[1:].islower())   # CamelCase 单词
```

## 4. 备选方案与取舍

| 方案 | 结论 | 理由 |
|---|---|---|
| 用 LLM 抽实体 | ❌ | 写入热路径已有两次 LLM，成本/延迟线性上涨 |
| 收所有 `eng` token | ❌ | `and`/`for`/`the`/`ok` 全进图，图上会被噪声淹没 |
| 只收全大写 | ❌ | 漏 `Kafka`/`PyCharm` 这类 CamelCase |
| 大写 + CamelCase + 停用词 | ✅ **采用** | 覆盖面与噪声的平衡点 |

## 5. 测试策略

| 层 | 覆盖 | 手段 |
|---|---|---|
| 单测 | 正例（ESB/Kafka/MQ/PyCharm）、反例（API/and/esb）、去重（项目A） | 直接调 `extract_entities` |
| 回归 | 中文人名/地名/机构、名册优先、降级路径 | 复用 035 的用例集 |
| 真库 | ESB 节点与 `MENTIONS` 边 | 重灌 10-30 后 Cypher 断言 |

## 6. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 停用词不全 | 图上出现 `TODO`/`NULL` 之类噪声 | 首版 30+ 项，上线后按实际增补 |
| 小写缩写漏收（`esb`） | 少一条边，不影响正确性 | 接受；比引入噪声划算 |
| 与 033 震荡扫描交互 | `ESB`/`ESB2` 编辑距离 1，可能被扫进震荡 | 观察；必要时在扫描里排除全 ASCII 名对 |
| 英文实体参与图召回 | 查询 `ESB` 能命中，但也会命中 `ESB2` | 与中文实体同一套召回逻辑，先观察 |
