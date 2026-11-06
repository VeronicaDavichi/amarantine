---
phase: tasks
---

# 038 任务：称呼句式 + 摘要护栏 + 别名边

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 设计：[design.md](design.md)

## 任务清单

- [ ] **T1 基线脚本**
  - 记录当前状态：`王勇` 事件数、`王老师` 事件数、两者间边数、全库 `ALIAS_OF` 条数
  - 引用：REQ-1（反证）
  - 验证：脚本输出 `王勇=88, 王老师=8, alias_of=1(脏)`，退出码 0

- [ ] **T2 称呼句式（REQ-1）**
  - `labeler.py` 增 `_APPELLATION_PATTERNS`（三种句式），与 `_ALIAS_PATTERN` 合并产出
  - 引用：REQ-1
  - 验证：单测 `称呼王勇为王老师` → claim(alias=王老师, canonical=王勇)

- [ ] **T3 摘要护栏（REQ-3、REQ-4）**
  - `extract_alias_claims_verbose` 增加 `source` 参数（`utterance` / `summary`）；
    摘要模式要求两侧 `_is_clean_name`，且拒绝「说明/表示/提到」开头的左值
  - 引用：REQ-3、REQ-4
  - 验证：单测 `任泽昂说明就是这个接口` → 0 claim + 1 reject(reason=summary_prose)

- [ ] **T4 建边（REQ-2）**
  - 两侧都是 `Person`/名册成员 → `MERGE (alias)-[:ALIAS_OF {label:'exact'}]->(canonical)`
  - 引用：REQ-2
  - 验证：写入「称呼王勇为王老师」后，图上存在该边，label=exact

- [ ] **T5 查询侧展开（REQ-2）**
  - 图通道查人时走 `[:ALIAS_OF*0..1]` 展开（只对 Person，深度 1）
  - 引用：REQ-2
  - 验证：查「王勇」能召回王老师名下的 2 张 `前端` 卡

- [ ] **T6 脏边清理（REQ-5）**
  - `harness/clean_alias_edges.py`：dry-run 列出 `label='uncertain' && conf<0.5` 的边 → `--apply` 删除
  - 引用：REQ-5
  - 验证：清理后全库 `ALIAS_OF` 只剩 exact 边；`接口 → 任泽昂说明` 消失

- [ ] **T7 测试 + 文档 + 真库验证**
  - 全量 pytest；`docs/user-manual.md` 补「称呼归一」一节
  - 引用：全部 REQ
  - 验证：A1–A6 全绿；真库能回答「前端开发是谁 → 王勇」

## 验收映射（需求 → 测试）

| 验收 | 覆盖 REQ | 手段 |
|---|---|---|
| A1 `称呼王勇为王老师` 抽出 claim，方向为 alias→canonical | REQ-1 | 单测 |
| A2 `叫他王老师` / `王勇（王老师）` 同样生效 | REQ-1 | 单测 |
| A3 `任泽昂说明就是这个接口` 抽 0 条，且 `alias_reject` 有 `summary_prose` 行 | REQ-3、REQ-4 | 单测 + SQLite 断言 |
| A4 图上 `(王老师)-[:ALIAS_OF {label:'exact'}]->(王勇)` 存在 | REQ-2 | 真库 Cypher |
| A5 查「王勇」能召回含「前端」的 2 张卡 | REQ-2 | 真库/召回脚本 |
| A6 清理后 `ALIAS_OF` 无 uncertain 边 | REQ-5 | 脚本 + Cypher 计数 |

## 完成定义（DoD）

- [ ] A1–A6 全绿，全量 `pytest` 无回归（002/003/035 用例尤其不能红）
- [ ] `harness/check_guide.py` 仍 `OK`
- [ ] `check_spec.py` 对本 spec 报 PASS（多文件模式）
- [ ] 真库报告：王勇/王老师 合边 + 「前端是谁」可检索

## 进展记录

### 2026-11-06 · 现象记录（spec 建立时）

- `Person` 共 38 个，其中称呼/噪声节点：`王老师`(8)、`刘老师`(1)、`季总`(2)、
  `今天下午`(1)、`道谢`(1)、`修正`(2)
- 全库 `ALIAS_OF` **1 条**，且为脏边：`接口 --uncertain(0.4)--> 任泽昂说明`
- 触发场景：灌完 `78497500999` 后问「前端开发是谁」，`王勇` 与 `前端` 零共现
