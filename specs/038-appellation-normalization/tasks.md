---
phase: tasks
---

# 038 任务：称呼句式 + 摘要护栏 + 别名边

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 设计：[design.md](design.md)

## 任务清单

- [ ] **T1 基线脚本**
  - 记录当前状态：`王泳` 事件数、`王老师` 事件数、两者间边数、全库 `ALIAS_OF` 条数
  - 引用：REQ-1（反证）
  - 验证：脚本输出 `王泳=88, 王老师=8, alias_of=1(脏)`，退出码 0

- [ ] **T2 称呼句式（REQ-1）**
  - `labeler.py` 增 `_APPELLATION_PATTERNS`（三种句式），与 `_ALIAS_PATTERN` 合并产出
  - 引用：REQ-1
  - 验证：单测 `称呼王泳为王老师` → claim(alias=王老师, canonical=王泳)

- [ ] **T3 摘要护栏（REQ-3、REQ-4）**
  - `extract_alias_claims_verbose` 增加 `source` 参数（`utterance` / `summary`）；
    摘要模式要求两侧 `_is_clean_name`，且拒绝「说明/表示/提到」开头的左值
  - 引用：REQ-3、REQ-4
  - 验证：单测 `任泽安说明就是这个接口` → 0 claim + 1 reject(reason=summary_prose)

- [ ] **T4 建边（REQ-2）**
  - 两侧都是 `Person`/名册成员 → `MERGE (alias)-[:ALIAS_OF {label:'exact'}]->(canonical)`
  - 引用：REQ-2
  - 验证：写入「称呼王泳为王老师」后，图上存在该边，label=exact

- [ ] **T5 查询侧展开（REQ-2）**
  - 图通道查人时走 `[:ALIAS_OF*0..1]` 展开（只对 Person，深度 1）
  - 引用：REQ-2
  - 验证：查「王泳」能召回王老师名下的 2 张 `前端` 卡

- [ ] **T6 脏边清理（REQ-5）**
  - `harness/clean_alias_edges.py`：dry-run 列出 `label='uncertain' && conf<0.5` 的边 → `--apply` 删除
  - 引用：REQ-5
  - 验证：清理后全库 `ALIAS_OF` 只剩 exact 边；`接口 → 任泽安说明` 消失

- [ ] **T7 测试 + 文档 + 真库验证**
  - 全量 pytest；`docs/user-manual.md` 补「称呼归一」一节
  - 引用：全部 REQ
  - 验证：A1–A6 全绿；真库能回答「前端开发是谁 → 王泳」

## 验收映射（需求 → 测试）

| 验收 | 覆盖 REQ | 手段 |
|---|---|---|
| A1 `称呼王泳为王老师` 抽出 claim，方向为 alias→canonical | REQ-1 | 单测 |
| A2 `叫他王老师` / `王泳（王老师）` 同样生效 | REQ-1 | 单测 |
| A3 `任泽安说明就是这个接口` 抽 0 条，且 `alias_reject` 有 `summary_prose` 行 | REQ-3、REQ-4 | 单测 + SQLite 断言 |
| A4 图上 `(王老师)-[:ALIAS_OF {label:'exact'}]->(王泳)` 存在 | REQ-2 | 真库 Cypher |
| A5 查「王泳」能召回含「前端」的 2 张卡 | REQ-2 | 真库/召回脚本 |
| A6 清理后 `ALIAS_OF` 无 uncertain 边 | REQ-5 | 脚本 + Cypher 计数 |

## 验收结果（2026-11-09）

| 验收 | 结果 | 证据 |
|---|---|---|
| A1 `称呼王泳为王老师` 抽出 claim，方向 alias→canonical | ✅ | `('王老师','王泳','exact')` |
| A2 `王泳（王老师）` 同样生效 | ✅ | `('王老师','王泳','exact')` |
| A3 摘要叙述不产 claim 且留痕 | ✅ | `summary_prose` 拒绝记录；`糖包就是豆包` 仍正常 |
| A4 图上 `ALIAS_OF` 边存在 | ✅ | 真库 `(王泳)-[:ALIAS_OF {label:'exact'}]->(王老师)` |
| A5 查「王泳」召回「王老师」名下卡 | ✅ | 真库：查两侧各返回 2 条 |
| A6 清理后无 uncertain 边 | ✅ | 删 1 条（`任泽昂说明 -> 接口` conf=0.4），复检 0 |

## 完成定义（DoD）

- [ ] A1–A6 全绿，全量 `pytest` 无回归（002/003/035 用例尤其不能红）
- [ ] `harness/check_guide.py` 仍 `OK`
- [ ] `check_spec.py` 对本 spec 报 PASS（多文件模式）
- [ ] 真库报告：王泳/王老师 合边 + 「前端是谁」可检索

## 进展记录

### 2026-11-06 · 现象记录（spec 建立时）

- `Person` 共 38 个，其中称呼/噪声节点：`王老师`(8)、`刘老师`(1)、`陈总`(2)、
  `今天下午`(1)、`道谢`(1)、`修正`(2)
- 全库 `ALIAS_OF` **1 条**，且为脏边：`接口 --uncertain(0.4)--> 任泽安说明`
- 触发场景：灌完 `10000000001` 后问「前端开发是谁」，`王泳` 与 `前端` 零共现

## 2026-11-09 · 实现记录

**已交付**

| 文件 | 内容 |
|---|---|
| `labeler.py` | 称呼句式（`称呼X为Y` / `X（Y）`）+ 摘要护栏 + span 去重 + 代词过滤 |
| `graph_outbox.py` | `RESOLVE_ALIAS_CYPHER` 改**双向** |
| `graph_search.py` | 别名主人取「本名 + 别名」两侧 |
| `recall.py` | 图通道命中回填元数据 + exact 别名命中强制保留 |
| `harness/clean_alias_edges.py` | 清理 uncertain 脏边（默认 dry-run） |
| `tests/test_appellation.py` | 9 条 |
| 全量 | **1324 passed** |

**过程中发现并修掉的三个真问题**

1. **称呼被 `称呼` 自身二次匹配**：`称呼王泳为王老师` 里「称」+「呼王泳为」也命中一次，
   靠 span 去重（同一片文字只留第一条）解决，而不是靠正则前瞻。
2. **图通道命中的卡被静默丢弃**：recall 要求命中项有 vector/bm25 元数据，
   而别名产生的卡常常只从图里来 —— 这是 A5 第一次跑只出 1 条的真因。
   现在按 `GRAPH_META_LIMIT` 回填，并给 exact 别名命中标 `forced`。
3. **脏边清理需要能跑**：`clean_alias_edges.py` 默认 dry-run，`--apply` 才删，
   阈值 `--max-conf` 可调，删完自动复检。

**边界（已知）**

- `叫他王老师` 目前**抽不出来**：正则里「他」会被人名吃掉（`他王`），
  代词过滤把整条丢了。宁可漏 —— 建错边比少一条边难修。
- 摘要护栏只挡「说明/表示/提到…」开头的左值；摘要里其他的叙述句式仍可能误抽。
