---
phase: tasks
---

# 043 任务：身份数据采集 → 名册扩展 → uid 锚定

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 设计：[design.md](design.md)

## 任务清单

- [x] **T1 身份数据采集**
  - 从本地库抽 `tbuser_profile_v2(uid, nick)` + `tbuser_alias_name(uid, alias)` → `identity.json`
  - 备注来源另查（`contact_friend` 为 0 行），查不到就在报告里明确写「不可得」
  - 引用：REQ-1
  - 验证：输出 469 条 uid/nick + 1 条 alias；脚本退出码 0

- [x] **T2 备注解析**
  - `姓名 - 说明` / `（说明）` / `/` 三种分隔；姓名部分过 `_is_clean_name`
  - 引用：REQ-2
  - 验证：`王晓虹 - 加班问题人事` → 姓名 `王晓虹`、说明 `加班问题人事`

- [x] **T3 结构化名册**
  - `IngestRequest.roster` 支持 `[{uid, names[], remark, source}]`（向后兼容纯字符串数组）
  - 引用：REQ-1、REQ-3
  - 验证：旧格式仍可用；新格式能带 uid

- [x] **T4 优先级合并**
  - 按 uid 聚合多来源，按 `备注 > 昵称 > 真名` 取名；冲突写 `name_history`
  - 引用：REQ-1、REQ-5
  - 验证：同 uid 两昵称 → 一个人 + 1 条历史；同名两 uid → 两个 Account

- [x] **T5 Account 节点**
  - 写 `(:Account {uid, platform, name_history})` + `[:OPERATED_BY]`
  - 引用：REQ-4
  - 验证：真库中存在 Account 节点且与 Person 相连

- [x] **T6 短名字用例集**
  - 从 150 个两字昵称抽 10 个，覆盖「名册存在 → person / 不存在 → topic」
  - 引用：REQ-3
  - 验证：10 个全部符合预期，无因长度被丢

- [x] **T7 非汉族检测 + 报告**
  - `·` / 非汉字 / 长且非百家姓首字 → 不建 Person，写报告
  - 引用：REQ-6
  - 验证：构造 `阿卜杜拉·买买提` → topic + 报告行

- [x] **T8 测试与文档**
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

- [x] A1–A7 全绿（A4 的改名分裂由 [045](../045-person-key-strategy/) 解决）；全量 pytest 绿
- [x] `check_spec.py` 对本 spec PASS
- [x] 真库报告：Account 3 个 / OPERATED_BY 3 条（临时 ns `verify043`，已清理）
- [x] 「未识别姓名」报告非空：导出时标出 23 个非人名账号（钉钉小秘书等）

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

## 验收结果（2026-11-09）

| 验收 | 结果 | 证据 |
|---|---|---|
| A1 备注解析出姓名/说明 | ✅ | `parse_remark("王晓虹 - 加班问题人事")` → `("王晓虹","加班问题人事")`；括号形 `王晓虹（合规）` → `("王晓虹","合规")` |
| A2 两字昵称成为 `Person` | ✅ | 真库：`王伟民` / `孙柏林` / `刘阅` 三个名册成员全部落成 `Person` |
| A3 名册外名字降级 `topic` | ✅ | 035 的既有行为未变（回归用例仍绿） |
| A4 同 uid 改名 → 1 个 Person | ✅ **已由 045 收口** | 045 把 Person 键改成 `uid:<uid>`；真库验证「同 uid 两次写入 → 1 个 Person，旧名进 `previous_names`」 |
| A5 同名不同 uid → 2 个 Account | ✅ | 单测 `test_same_nick_different_uid_creates_two_accounts` |
| A6 非汉族姓名不建 Person + 报告 | ✅ | **已返工为四分类**（见下）；导出：service 20 / placeholder 3 / ethnic 0，真人剥名 3 个 |
| A7 品牌仍为 topic | ✅ | 035 回归用例 |

复核命令：

```bash
cd back_end
.venv-linux/bin/python3 -m pytest tests/test_identity.py -q   # 30 passed
.venv-linux/bin/python3 -m pytest -q                          # 1291 passed
python ../harness/export_identity.py                          # 474 条身份
```

## 2026-11-09 · 实现记录

**已交付**

| 文件 | 内容 |
|---|---|
| `src/memory/services/identity.py` | 备注解析、非汉族检出、`roster` 双形态、`Account`/`OPERATED_BY` 写入 |
| `tests/test_identity.py` | 30 条（解析 / 判据 / 双形态 / 幂等 / 同名不同 uid / 图故障降级） |
| `harness/export_identity.py` | 从钉钉库导身份（474 条，23 个非人名标出） |
| `harness/seed_dingtalk.py` | 新增 `--identity`，按本批出现的人过滤后带 uid 提交 |
| `admin_http.py` | `IngestRequest.roster` 收 `list[str \| dict]`；写账号层失败只告警 |

**已知偏差：REQ-5「改名不产生第二个 Person」未完全满足。**

`Account` 的 MERGE 键是 `(namespace, uid)`，但 `Person` 的键是 `key_for(name)` ——
改名后新名字会建出第二个 `Person`，两者只共享同一个 `Account`。

- 已做到的：`Account.nicks` 保留全部历史昵称（改名不丢旧名，旧卡片仍能对上）
- 未做到的：旧 `Person` 与新 `Person` 之间没有 `ALIAS_OF`
- 为什么没顺手改：`Person.key` 由名字派生是**全局约定**（事件卡的 `INVOLVES` 边、别名边、
  图重建都依赖它）。要改成 uid 派生，等于重做实体身份策略，必须单开 spec ——
  硬塞进 043 会把「身份来源分层」和「实体键策略」两件事搅在一起。

**已单开**：[045 Person 键策略](../045-person-key-strategy/) —— `resolve_person_key`（uid 优先）+ 一次性迁移脚本，负责合并同一 uid 的多个名字节点。

### 2026-11-09 · 半截词修复（A6 收尾）

**现象**：`王者荣耀` 被 jieba 切成 `王者`(n) + `荣耀`(nr)，`荣耀` 凭 `nr` 冒充人；
无名单时直接变成 `Person`，有名单时也留下 `topic` 噪声。

**判据**（`labeler._extract_with_jieba`）：`nr` 的**前一个 token 是名词**
（`n` / `nz` / `ns` / `vn`，且 ≥2 字）→ 它是词的一半，降级为 `topic`。
人名前面只会是动词 / 连词 / 介词（`张三让`、`王五和`、`跟王老师`），不会是名词。

**实测**：

```
王者荣耀这个游戏挺好玩的   → [('荣耀','topic')]      ← 修前是 person
昨天和王者荣耀一起开的会   → [('荣耀','topic')]
张三让李四去做项目A        → 张三/李四 仍为 person    ← 不误伤
```

**已知边界**：`荣耀说这个方案可以`（句首、无名词前缀）仍判为 person ——
无上下文的单句本来就分不清，有名单时由准入兜底。

**测试**：`test_labeler.py` 新增 3 条；全量 1295 passed。

### 2026-11-09 · A6 返工：从「非汉族」改为四分类

**为什么要返工**：旧判据把「含字母」等同于「非汉族姓名」，23 个标记里
**8 个是真的人**被扫出名册（`BOB洪亚军`、`王政博Yves`、`22传32马雯卓`、
`五一班和子轩同学家长`、`Gavin`、`nice`、`Phoebe.Ding`、`aaa`），
它们由「该不该当人」的判断问题，被误当成「姓名构词」问题。

**新判据**（`identity.classify_identity_name`，四类）：

| 类 | 判据 | 例子 | 处理 |
|---|---|---|---|
| `service` | 含标记词（助手/机器人/小秘书/管家/工资条/助理…） | 钉钉小秘书、AI助理 | 不入名册 |
| `ethnic_name` | 含 `·`，或 ≥5 汉字且首字非百家姓 | 阿卜杜拉·买买提 | 不入名册（暂不解析） |
| `placeholder` | 无信息量（单字、纯重复小写、纯数字） | aaa、王 | 不入名册 |
| `person` | 其余；带修饰的先剥首尾/取最长汉字段 | BOB洪亚军→洪亚军 | **入名册** |

**关键修正**：含英文**不再等于**非汉族姓名。剥出中文名的是真人，
纯英文昵称（`Gavin`）默认也是真人 —— 昵称难看是显示问题，不是身份问题。

**导出实测**（474 条身份）：

```
person 451 / service 20 / placeholder 3 / ethnic_name 0
剥掉修饰的真人 3 个：BOB洪亚军→洪亚军、22传32马雯卓→马雯卓、王政博Yves→王政博
```

**拒绝要带原因**：`split_roster()` 的第三项从 `list[str]` 改为
`[{"name": ..., "kind": "service|ethnic_name|placeholder"}]` —— 报告能回答「为什么没进名册」。

**审计报告**：`harness/export_identity.py` 现在额外产出
`back_end/data/corpus/identity_audit.json`（分类统计 + 三类明细 + 剥名对照表）。

**测试**：新增 22 条（服务号/修饰名/纯英文/占位/原因）；`test_identity.py` 49 passed；
全量 **1314 passed**。

**端到端**：真库临时 ns `verify043b` 灌 4 条 → Account 3 个（刘阅/孙柏林/王伟民）、
Person 3 个，与名册一致；已清理。

### 2026-11-09 · 收尾

A4 的偏差（改名产生第二个 `Person`）已由 [045 Person 键策略](../045-person-key-strategy/) 解决：
Person 的键从 `key_for(name)` 改成 `uid:<uid>`，改名只更新 `name` 并把旧名并入
`previous_names`，不再新建节点。

**本 spec 到此完成**：身份来源分层（uid > 备注 > 真名 > 昵称 > jieba）、
四分类防误伤、Account/OPERATED_BY 落图，三项都已在真库验证。
