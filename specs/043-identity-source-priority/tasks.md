---
phase: tasks
---

# 043 任务：身份数据采集 → 名册扩展 → uid 锚定

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 设计：[design.md](design.md)

## 任务清单

- [ ] **T1 身份数据采集**
  - 从本地库抽 `tbuser_profile_v2(uid, nick)` + `tbuser_alias_name(uid, alias)` → `identity.json`
  - 备注来源另查（`contact_friend` 为 0 行），查不到就在报告里明确写「不可得」
  - 引用：REQ-1
  - 验证：输出 469 条 uid/nick + 1 条 alias；脚本退出码 0

- [ ] **T2 备注解析**
  - `姓名 - 说明` / `（说明）` / `/` 三种分隔；姓名部分过 `_is_clean_name`
  - 引用：REQ-2
  - 验证：`王晓虹 - 加班问题人事` → 姓名 `王晓虹`、说明 `加班问题人事`

- [ ] **T3 结构化名册**
  - `IngestRequest.roster` 支持 `[{uid, names[], remark, source}]`（向后兼容纯字符串数组）
  - 引用：REQ-1、REQ-3
  - 验证：旧格式仍可用；新格式能带 uid

- [ ] **T4 优先级合并**
  - 按 uid 聚合多来源，按 `备注 > 昵称 > 真名` 取名；冲突写 `name_history`
  - 引用：REQ-1、REQ-5
  - 验证：同 uid 两昵称 → 一个人 + 1 条历史；同名两 uid → 两个 Account

- [ ] **T5 Account 节点**
  - 写 `(:Account {uid, platform, name_history})` + `[:OPERATED_BY]`
  - 引用：REQ-4
  - 验证：真库中存在 Account 节点且与 Person 相连

- [ ] **T6 短名字用例集**
  - 从 150 个两字昵称抽 10 个，覆盖「名册存在 → person / 不存在 → topic」
  - 引用：REQ-3
  - 验证：10 个全部符合预期，无因长度被丢

- [ ] **T7 非汉族检测 + 报告**
  - `·` / 非汉字 / 长且非百家姓首字 → 不建 Person，写报告
  - 引用：REQ-6
  - 验证：构造 `阿卜杜拉·买买提` → topic + 报告行

- [ ] **T8 测试与文档**
  - 单测 + 真库验证；`docs/user-manual.md` 补「身份来源与优先级」
  - 引用：全部 REQ
  - 验证：全量 pytest 绿；A1–A7 全过

## 验收映射

| 验收 | 覆盖 REQ | 手段 |
|---|---|---|
| A1 备注解析出姓名/说明两段 | REQ-2 | 单测 |
| A2 两字昵称入名册后成为 `Person`（`王泳`） | REQ-3 | 单测 + 真库 |
| A3 名册外两字名降级 `topic`，不冒充人 | REQ-3、不变行为 | 单测 |
| A4 同 uid 改名 → 1 个 Person + name_history | REQ-5 | 图查询 |
| A5 同名不同 uid → 2 个 Account，不合并 | REQ-4 | 图查询 |
| A6 `阿卜杜拉·买买提` 不建 Person，进报告 | REQ-6 | 单测 |
| A7 品牌（`周大福`）在名册存在时仍为 topic | 不变行为 | 回归用例 |

## 完成定义（DoD）

- [ ] A1–A7 全绿；全量 pytest 无回归（1223 基线）
- [ ] `check_spec.py` 对本 spec PASS
- [ ] 真库报告：Account 节点数、Person 来源分布（uid/remark/nick）
- [ ] 「未识别姓名」报告非空（证明检测生效，不是没跑）

## 进展记录

### 2026-11-06 · 名册已从「优先」升级为「准入」（代码已改）

**触发**：`周大福` / `王老吉` 被 jieba 标成 `nr`，当成了人；而 `陈柏林` 反过来被切坏。
两者是**同一个问题的正反面** —— 字符串本身不携带「是不是人」的信息。

**改动**（`labeler.py`）：`extract_entities` 在 `roster_names()` 非空时，
把 **jieba 抽出的** `person` 降级为 `topic`；名册命中仍是 `person`；无名单行为不变。

```
                     无名册              有名册[王泳,陈柏林,刘佳]
周大福今天开业        person              topic
王老吉的广告不错      person              topic
陈柏林询问…           柏林(person)        陈柏林(person)
欧阳锋说了一句话      person              topic      ← 不冒充人
```

**取舍**：名册外的人名降级为 `Entity`，信息不丢但不再冒充 `Person`。

**测试**：`tests/test_roster_names.py` 新增 4 条；全量 **1223 passed**。

> 注：035 已是 `complete`，按「只改非 complete/archived」的约定，这条行为变更记录
> 记在本 spec（043）而不是 035。

### 2026-11-06 · 数据源实测（spec 建立时）

```
tbuser_profile_v2   469 行  uid + nick（alias 全空）
tbuser_alias_name     1 行  「王晓虹 - 加班问题人事」
contact_friend        0 行  remark 字段存在但无数据
两字昵称            150 个（32%）
含 ·/英文昵称        12 个
```

**结论**：昵称可用、备注**当前不可得**、少数民族姓名样本为 0（但类别存在）。
