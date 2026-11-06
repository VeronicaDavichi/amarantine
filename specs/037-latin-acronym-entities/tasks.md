---
phase: tasks
---

# 037 任务：英文缩写抽取通道

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 设计：[design.md](design.md)

## 任务清单

- [ ] **T1 基线脚本**
  - 记录当前图上的 Entity 清单与英文节点数（应为 0），可复跑
  - 引用：REQ-1（反证现状）
  - 验证：脚本输出 `entities=11 english=0`，退出码 0

- [ ] **T2 判据与停用词**
  - `labeler.py` 增 `_ACRONYM` / `_STOP_ACRONYMS` / `_looks_like_acronym`
  - 引用：REQ-2
  - 验证：单测对 `ESB`/`Kafka`/`MQ`/`PyCharm` 返回 True，对 `API`/`and`/`esb`/`123` 返回 False

- [ ] **T3 接入第三条通道**
  - `_extract_entities` 中按「`_CODED_PATTERN` → 后缀扫描 → 缩写（跳过占位区间）」顺序
  - 引用：REQ-1、REQ-3、REQ-4
  - 验证：`项目A` 只出一条 topic；`extract_entities` 仍被查询侧复用（无新函数）

- [ ] **T4 测试（见验收映射）**
  - 引用：全部 REQ
  - 验证：`pytest -q` 全绿，新增用例对应 A1–A6

- [ ] **T5 真库验证**
  - 重灌含 ESB 的批次（依赖 036 的分片修复），确认节点与边
  - 引用：REQ-1、REQ-4
  - 验证：`MATCH (e:Event)-[:MENTIONS]->(x:Entity {name:"ESB"}) RETURN count(*)` > 0

- [ ] **T6 文档**
  - `docs/user-manual.md` 抽实体一节补「英文缩写通道 + 停用词表」
  - 引用：REQ-2
  - 验证：`harness/check_guide.py` 输出 `OK: 文档与代码一致`

## 验收映射（需求 → 测试）

| 验收 | 覆盖 REQ | 手段 |
|---|---|---|
| A1 `ESB接口返回字段的数据字典` → 含 `ESB` | REQ-1 | 单测 |
| A2 `API网关限制了模型大小` → 不含 `API` | REQ-2 | 单测（停用词） |
| A3 `项目A 的系统日志` → `项目A` 一条、kind 仍 topic | REQ-3、不变行为 | 单测（去重） |
| A4 `PyCharm` / `Kafka` / `MQ` / `CIF` 各出 1 条 | REQ-2 | 参数化单测 |
| A5 `陈柏林询问…` → `陈柏林` | 不变行为 | 复用 035 用例 |
| A6 真库 `MENTIONS→ESB` 计数 > 0 | REQ-1、REQ-4 | 真库脚本 |

## 完成定义（DoD）

- [ ] A1–A6 全绿，全量 `pytest` 无回归（尤其 035 的名册用例）
- [ ] `harness/check_guide.py` 仍 `OK`
- [ ] `check_spec.py` 对本 spec 报 PASS（多文件模式）
- [ ] 真库报告：ESB 节点 + 至少一条 `MENTIONS` 边
