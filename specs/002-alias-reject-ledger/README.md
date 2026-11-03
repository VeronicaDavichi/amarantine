---
status: complete
created: 2026-11-03
priority: high
tags:
- alias
- labeler
- memory
- backend
- observability
created_at: 2026-11-03T01:05:59.796974162Z
updated_at: 2026-11-03T01:06:12.340321625Z
completed_at: 2026-11-03T01:06:12.340321625Z
transitions:
- status: complete
  at: 2026-11-03T01:06:12.340321625Z
---
# 别名判据治本：拒绝留痕 + 罕见姓豁免

> **Status**: in-progress · **Priority**: high · **Created**: 2026-11-03

## Overview

别名声明从「用户原话」到图上一条 `ALIAS_OF` 边，中间有两道判据：

```
原话 ──extract_alias_claims──> claim ──graph_outbox──> 边
            │                              │
            └ 左值不像名字 → continue      └ 右值不像名字 → 跳过
              （连 claim 都不生成）          （claim 在卡上，边没有）
```

**两道都是布尔过滤，丢弃之后不留任何痕迹。** 于是同一个模式犯了三次：

| # | 漏掉的声明 | 当时判据的问题 | 怎么发现的 |
|---|---|---|---|
| 1 | `三哥就是印度人` | 单字「人」在描述标记集合里 | 两个会话后查真库 |
| 2 | `老王就是科学家` | 单字「家」在同一个集合里 | 同一个错误又犯一次 |
| 3 | `种师道也叫老种` | 首字 `种` 在量词集合里 | 用户问「能不能靠 LLM 补」 |

前两次是判据写错（单字做包含匹配，已在上一个提交修掉）；第三次是判据写对了
但个案被牺牲（`种` 确实是量词，`小明就是个老实人` 必须拦）。

真正的结论不是「判据要更准」，而是：**判错了不要紧，判错了还看不见才要命。**
判据永远有盲区，所以必须有一条看得见、可复核、可修的漏出通道。

**目标**：每一次拒绝都带理由落库，并给两类已知盲区补豁免。出口是 A1–A12 全绿。

**非目标**

| 不做 | 理由 / 归属 |
|---|---|
| 把 `accepted` 记录自动补建边 | ~~要重走写入链路~~ → **这句是错的**。补建只需 `AliasClaim`，`event_id` 只作边的 `source_event_id` 属性，与内容哈希无关。已在 `003-admin-closure` 实现 |
| 改 `event_id` 哈希口径 | D1 已定，改了幂等就废 |
| 引入 alembic | 全项目 SQLite 表都是服务自建 `CREATE TABLE IF NOT EXISTS` |
| 姓氏作用域隔离到 namespace | `labeler` 是纯文本层、无 namespace 概念（与 jieba 词典同为进程级） |
| 用 jieba 词性当通用判据 | 标签集过细且不一致，同一字单用与合用标签不同；只用于「整串是单一 `nr` 词条」 |
| 完整姓氏字表 | `_LEADING_MEASURES` 只有 `个/位/种`，只需回答「这三个里哪个是姓氏」 |

## Design

### 落点 1 · 判据会解释自己

名字判据收敛成唯一实现 `_judge_name() -> NameVerdict`，
`_is_clean_name` / `_looks_like_name` 退化成布尔壳（判据只写一遍）。

```
NameVerdict
  ok     放不放行
  reason empty / too_long / marker / leading / ending
  detail 命中的标记、开头的字、结尾的字、长度
```

检查顺序固定：`空 → 长度>6 → 描述标记 → 开头 → 结尾`。

**`detail` 必须确定性。** `_DESCRIPTIVE_MARKERS` 是 `frozenset`，直接
`next(iter(...))` 拿到哪个标记取决于字符串哈希种子（每进程不同），而 `detail`
要落库 —— 随机等于「同一句话两次记下不同理由」。`_first_marker()` 的排序键
写死：**最早出现 → 最长 → 字典序最小**。

### 落点 2 · 两侧都留痕

三处拒绝点全部落库：

| 位置 | 典型输入 | 原来 | 现在 |
|---|---|---|---|
| 左值 | `三哥主打的就是有声有色有味` | `continue`，连声明都不生成 | `side=alias` |
| 右值 | `糖包就是这种人` | 声明在卡上，建边时静默跳过 | `side=canonical` |
| LLM 决议后 | resolver 改写过仍不合判据 | 完全无声 | `side=canonical` |

**左值最隐形**：连 claim 都不生成，LLM 复核层根本看不到 —— `种师道也叫老种` 就这么丢的。

```sql
CREATE TABLE IF NOT EXISTS alias_reject (
    namespace TEXT NOT NULL, event_id TEXT NOT NULL,
    alias TEXT NOT NULL, canonical TEXT NOT NULL, side TEXT NOT NULL,
    reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    source_text TEXT, created_at TEXT NOT NULL,
    resolved_at TEXT, resolution TEXT,
    PRIMARY KEY (namespace, event_id, alias, canonical, side)
);
```

`append_alias_edges` 返回值 `int` → `AliasWriteResult(written, skipped)`；
`OutboxResult.alias_skipped` 由 `remember` 两条写入路径消费。
`record_safe()` 吞异常只告警 —— 留痕是旁路，绝不能阻断记忆写入。

### 落点 3 · 罕见姓豁免

`种` 既是量词（`一种东西`）又是姓氏（`种师道`，读 chóng，汉典有证）。两道闸：

1. **量词字本身得是姓氏**（`_MEASURE_SURNAMES`）。这层不能省 ——
   实测 jieba 的 HMM 把 `位小明` 也标成 `nr`，否则豁免会漏成筛子。
2. 有资格后任一成立即放行：
   - jieba 把**整串**切成单一 `nr` 词条（`种师道` → `[('种师道','nr')]`）
   - 用户声明过该姓，且名字 **≥3 字**（姓+名≥2）

用户声明来自 `姓X`（X 后面不接汉字，再排掉 `名`/`氏`），落表：

```sql
CREATE TABLE IF NOT EXISTS surname_decl (
    namespace TEXT NOT NULL, char TEXT NOT NULL,
    evidence TEXT, event_id TEXT, created_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (namespace, char)
);
```

`label_by_rules` 顺序改为 **parse → register → extract**，所以
`这人姓种，种小明就是老种` 的别名抽取已经知道「种」是姓，同句内立刻生效。

**只在「量词打头」这一条判据上豁免，别的判据一点没放宽。**

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/alias-rejects` | 列待处理，带 `by_reason` 分组计数 |
| POST | `/admin/alias-rejects/resolve` | 只标记，不补建边 |
| GET | `/admin/alias-surnames` | 已声明 + 进程内生效集合 |
| POST | `/admin/alias-surnames` | 立即生效 + 落库 |

启动时 `mcp_stdio` 与 `admin_http` 都调 `load_into_labeler(settings)`。

### 取舍

| 风险 | 处理 |
|---|---|
| 表被正常流量灌满 | 只记被拒的；正常声明不记（有测试断言 `count_pending==0`） |
| 留痕失败反而阻断写入 | `record_safe` 吞异常只告警 |
| 返回值改类型 → 老调用方静默错 | 给了 `__eq__`/`__int__`；调用方只有 `alias_apply` 一处 |
| `label_by_rules` 进程级副作用 | 有意的（同句生效）；conftest 加 autouse 每次 `reset_surnames()` |
| 姓氏跨 namespace 可见 | 已记录；姓氏不敏感，`labeler` 无 namespace 概念 |
| 2 字名字仍可能漏 | 接受 —— 会进 `alias_reject`，看得见能补，这正是本 spec 的产出 |

## Plan

- [x] T1 labeler：`_judge_name` / `NameVerdict` / 确定性 `_first_marker`
- [x] T2 labeler：`extract_alias_claims_verbose` + `LabelResult.alias_rejects`
- [x] T3 labeler：`_MEASURE_SURNAMES` + `SURNAME_DECL_RE` + `parse_surname_declarations`
- [x] T4 services：`alias_reject.py`（表 / `AliasRejectQueue` / `record_safe`）
- [x] T5 services：`surname_decl.py`（表 / `SurnameRegistry` / `load_into_labeler`）
- [x] T6 graph_outbox：`AliasSkip` / `AliasWriteResult` / `_partition_alias_claims`
- [x] T7 remember：`_record_alias_rejects` + `_record_surname_declarations`（旁路）
- [x] T8 entrypoints：两个启动点加载姓氏
- [x] T9 admin_http：四个接口
- [x] T10 tests：`test_alias_reject.py` 41 条、`test_admin_http.py` +8、conftest 夹具
- [x] T11 docs：user-manual 三节 + changelog v0.15

## Test

- [x] A1 `_judge_name` 五类各返回固定 reason，8 个参数化例子全过
- [x] A2 `detail` 取值固定（不随哈希种子变），断言到具体字符串
- [x] A3 `extract_alias_claims_verbose("三哥主打的就是有声有色有味")` → 0 claim + 1 reject，`side=alias`、`reason=ending`、`detail=的`
- [x] A4 写左值被拒的句子后，`alias_reject` 有 `side=alias` 行
- [x] A5 写右值被拒的句子后，`alias_reject` 有 `side=canonical` 行
- [x] A6 同一主键重复上报 → 清掉旧 `resolution`，`count_pending` 回到 1
- [x] A7 `这人姓种`→`["种"]`；`姓名`/`姓氏`/`老百姓`/`百家姓`→`[]`
- [x] A8 `种师道` 通过；`种人`/`种老师`/`个老实人`/`位小明` 全部 `reason=leading`
- [x] A9 声明后 `个小红` 通过、`个明` 仍被拒（≥3 字才算完整姓名）
- [x] A10 `load_into_labeler` 灌回进程并立刻影响判据
- [x] A11 全量 pytest 绿；`check_guide` 输出 `OK: 文档与代码一致`
- [x] A12 真库活体：三句例句的边与留痕符合预期，退出码 0

```
$ ./harness/dev-linux.sh pytest -q
789 passed, 1 warning in 86.92s                      exit 0

$ ./harness/dev-linux.sh python ../harness/check_guide.py
配置项 73 个 | 工具 4 个 | 文档变量 36 个
OK: 文档与代码一致                                    exit 0

$ ./harness/dev-linux.sh python ../harness/e2e_mcp.py
OK: MCP stdio 端到端全绿                              exit 0
```

A12 真库活体（`namespace=rejectfix67470`，真 Weaviate + 真 Neo4j）：

```
图上的 ALIAS_OF 边（1 条）:
  种师道 -> 老种 (label=exact, conf=1.0)

alias_reject（3 条）:
  side=alias     reason=ending   detail='的'    三哥主打的 -> 有声有色有味
  side=canonical reason=marker   detail='这种'   糖包 -> 这种人
  side=canonical reason=leading  detail='位'    小李 -> 位小明

surname_decl: ['种']
7/7 断言 PASS
```

## Notes

### 实现中发现、值得记一笔的五件事

1. **`_DESCRIPTIVE_MARKERS` 是 `frozenset`，`detail` 会随机。** 第一版直接
   `for marker in _DESCRIPTIVE_MARKERS` 取第一个命中，测试里同一个输入的
   `detail` 每次不一样 —— 字符串哈希种子每进程都变。
   **不是测试挑剔，是落库值不确定。**

2. **jieba HMM 会把 `位小明` 标成 `nr`。** 原设计「整串是单一 `nr` 就豁免」
   实测会把 `位小明` 放进来，所以加了「量词字本身得是姓氏」这第一道闸。
   探针：`种师道`/`种小明`/`种砚舟`/`种氏` → `nr`；`种人` → `n`；
   `种老师`/`种先生` → 双 token；**`位小明` → 也是 `nr`**。

3. **`姓X` 的正则 `(?![\u4e00-\u9fa5])` 挡不住 `姓名`。** 句末时「后面没有
   汉字」成立，`名` 照样被捕获。补了 `_NOT_SURNAME_CHARS = {"名","氏"}`。

4. **`A 就是 B` 的 canonical 是 B。** 判据检查 canonical，所以验证豁免必须让
   `种师道` 落在**右边**（`老种就是种师道`）。第一版写成
   `种师道就是老种`，边建出来了、断言也过了，但测的是干净词 `老种` ——
   **豁免根本没验到**，靠 A12 第一轮 FAIL 才暴露。

5. **`_strip_prefix` 会先削掉 `那个`/`这个`/`我说的`。**
   `糖包就是那个ai中最弱智的` 到判据手里已是 `ai中最弱智的`（7 字），
   撞的是长度上限而不是标记判据。

### 遗留

- `resolution="accepted"` 目前只标记、不补建边；补建要走重写链路，归 P3。
- `leanspec validate` 在本机报 `not yet migrated to the adapter API`；
  MCP 的 `validate_spec` 对 `status`/`priority` 报类型错（`001` 同样报）。
  两条校验路径当前都不可用，已按模板骨架人工对齐。

### 流程补记

本 spec 的代码先动工、spec 后补建，且首版是手写文件而非 `leanspec create`。
补建后已按 `.lean-spec/templates/spec-template.md` 骨架重排，并改用
`leanspec create --file` 生成 —— 之后新建 spec 一律走 `/leanspec-propose`。
