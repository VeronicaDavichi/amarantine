---
status: complete
priority: high
created: 2026-11-03
tags:
- alias
- labeler
- memory
- backend
- observability
epic: P0 长期记忆服务
breaking: true
created_at: 2026-11-03T00:53:55.051175100Z
updated_at: 2026-11-03T00:54:02.011144800Z
---
# 别名判据治本：拒绝留痕 + 罕见姓豁免

## 为什么开这个 spec

别名声明从「用户原话」到「图上一条 `ALIAS_OF` 边」，中间有两道判据过滤：

```
原话 ──extract_alias_claims──> claim ──graph_outbox──> 边
            │                              │
            └ 左值不像名字 → continue      └ 右值不像名字 → 跳过
              （连 claim 都不生成）          （claim 在卡上，边没有）
```

**两道都是布尔过滤，丢弃之后不留任何痕迹。** 于是同一个模式犯了三次：

| # | 漏掉的声明 | 根因（当时的判据） | 怎么发现的 |
|---|---|---|---|
| 1 | `三哥就是印度人` | `_DESCRIPTIVE_MARKERS` 里有单字「人」 | 两个会话后查真库 |
| 2 | `老王就是科学家` | 同一个集合里有单字「家」 | 同一个错误又犯一次 |
| 3 | `种师道也叫老种` | 首字 `种` 在量词集合里 | 用户问「能不能靠 LLM 补」 |

前两次是**判据写错了**（单字做包含匹配）；第三次是**判据写对了，但个案被牺牲**
（`种` 确实是量词，`小明就是个老实人` 必须拦）。

真正的教训不是「判据要更准」，而是：**判错了不要紧，判错了还看不见才要命。**
判据永远会有盲区，所以必须有一条看得见、可复核、可修的漏出通道。

## 目标

让每一次别名声明的拒绝都**带理由落库**，并给两类已知盲区补上豁免。

**出口（二值判定）**：A1–A12 全绿。

## 非目标

| 不做 | 理由 / 归属 |
|---|---|
| 把 `alias_reject` 里 `accepted` 的记录自动补建边 | 要重走写入链路（`event_id` 是内容哈希）。P3 |
| 改 `event_id` 哈希口径 | D1 已定，改了幂等就废了 |
| 引入 alembic | 全项目 SQLite 表都是「服务自己 `CREATE TABLE IF NOT EXISTS`」 |
| 姓氏作用域隔离到 namespace | `labeler` 是纯文本层、无 namespace 概念（跟 jieba 词典一样进程级）。已记录取舍 |
| 用 jieba 词性当通用判据 | 标签集太细且不一致（`zg`/`rz`/`b`/`ns`），同一个字单独出现和在组合里标签不同。只用于「整串是单一 `nr` 词条」这一窄判断 |
| 完整姓氏字表（几百字） | `_LEADING_MEASURES` 只有 `个/位/种` 三个字，只需要回答「这三个里哪个是姓氏」 |

## 设计：三个落点

### 落点 1 · 裁决带理由（`labeler.py`）

把名字判据收敛成**唯一实现** `_judge_name(candidate) -> NameVerdict`，
`_is_clean_name` / `_looks_like_name` 都退化成它的布尔壳。

```python
@dataclass(frozen=True)
class NameVerdict:
    ok: bool
    reason: str = NAME_OK      # ok / empty / too_long / marker / leading / ending
    detail: str = ""           # 命中的标记、开头的字、结尾的字、长度
```

检查顺序固定为：`空 → 长度>6 → 描述标记 → 开头 → 结尾`。

`_first_marker()` 的排序键固定为「最早出现 → 最长 → 字典序最小」——
`_DESCRIPTIVE_MARKERS` 是 `frozenset`，直接迭代的结果随字符串哈希种子
变化，而 `detail` 要落库，不确定就等于「同一句话两次记下不同理由」。

### 落点 2 · 两侧都留痕（新服务 `alias_reject.py`）

新表 `alias_reject`，主键 `(namespace, event_id, alias, canonical, side)`：

- `side="alias"` —— 左值被拒（`extract_alias_claims_verbose` 产出）
- `side="canonical"` —— 右值被拒（`graph_outbox._partition_alias_claims` 产出）

`graph_outbox.append_alias_edges` 的返回值从 `int` 改成
`AliasWriteResult(written, skipped)` —— 只为把「拒了哪些」带出去。
`OutboxResult` 加 `alias_skipped` 字段，`remember` 两条写入路径都消费它。

留痕是**旁路诊断**：`record_safe()` 包一层 `except`，它挂了绝不能让记忆写不进去
（跟 `remember._run_dual_check` 一个态度）。

### 落点 3 · 罕见姓豁免（`labeler.py` + 新服务 `surname_decl.py`）

`种` 是量词，也是姓氏（汉典：`●种 chóng — 姓。如宋代有种师道。`）。这类字
没法靠字本身区分，只能靠额外信息。两道闸：

1. **`_MEASURE_SURNAMES = {"种"}`** —— 量词字本身得是姓氏才有豁免资格。
   实测 jieba 的 HMM 会把 `位小明` 也标成 `nr`（对未登录词很激进），
   没有这层过滤，豁免就变成「量词后面跟个像名字的就行」。
2. 有资格之后，任一成立即放行：
   - jieba 把整串切成**单一 `nr` 词条**（`种师道` → `[('种师道','nr')]`）
   - 用户在 namespace 里声明过这个姓，且名字 **≥3 字**（姓+名≥2）

用户声明来源：`姓X` 且 X 后面**不接汉字**（`SURNAME_DECL_RE`），
再排掉 `名`/`氏`。`姓名`/`姓氏`/`老百姓`/`百家姓` 都不会误命中。

`label_by_rules` 的顺序是 **parse → register → extract**，所以
`这人姓种，种小明就是老种` 这一句里的别名抽取已经知道「种」是姓。

## 数据模型

```sql
CREATE TABLE IF NOT EXISTS alias_reject (
    namespace TEXT NOT NULL, event_id TEXT NOT NULL,
    alias TEXT NOT NULL, canonical TEXT NOT NULL, side TEXT NOT NULL,
    reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    source_text TEXT, created_at TEXT NOT NULL,
    resolved_at TEXT, resolution TEXT,
    PRIMARY KEY (namespace, event_id, alias, canonical, side)
);

CREATE TABLE IF NOT EXISTS surname_decl (
    namespace TEXT NOT NULL, char TEXT NOT NULL,
    evidence TEXT, event_id TEXT, created_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (namespace, char)
);
```

两张表都按现有模式：服务自己 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX`，
路径来自 `db_path_from_settings(settings)`，`threading.Lock` + 可注入 `clock`。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/alias-rejects` | 列待处理拒绝，带 `by_reason` 分组计数 |
| POST | `/admin/alias-rejects/resolve` | 标记已裁决（`accepted` = 判据错了） |
| GET | `/admin/alias-surnames` | 列已声明姓氏 + 进程内生效集合 |
| POST | `/admin/alias-surnames` | 声明姓氏（立即生效 + 落库） |

启动时 `mcp_stdio` 和 `admin_http` 都调 `load_into_labeler(settings)`。

## 验收（二值）

| # | 判定 |
|---|---|
| A1 | `_judge_name` 对空/超长/标记/开头/结尾五类各返回固定 `reason`，8 个参数化例子全过 |
| A2 | `detail` 取值固定（不随哈希种子变），断言到具体字符串 |
| A3 | `extract_alias_claims_verbose("三哥主打的就是有声有色有味")` 返回 0 claim + 1 reject，`side="alias"`、`reason="ending"`、`detail="的"` |
| A4 | `remember_event` 写左值被拒的句子后，`alias_reject` 有 `side="alias"` 的行 |
| A5 | `remember_event` 写右值被拒的句子后，`alias_reject` 有 `side="canonical"` 的行 |
| A6 | `alias_reject` 同一主键重复上报 → 清掉旧 `resolution`，`count_pending` 回到 1 |
| A7 | `parse_surname_declarations` 对 `这人姓种`→`["种"]`，对 `姓名`/`姓氏`/`老百姓`/`百家姓`→`[]` |
| A8 | `种师道` 通过；`种人`/`种老师`/`个老实人`/`位小明` 全部 `reason="leading"` 被拒 |
| A9 | 声明姓氏后 `个小红` 通过、`个明` 仍被拒（≥3 字才算完整姓名） |
| A10 | `load_into_labeler` 能把表里的姓氏灌回进程并立刻影响判据 |
| A11 | 全量 `pytest` 绿；`harness/check_guide.py` 输出 `OK: 文档与代码一致` |
| A12 | 真库活体验证：三句例句的边/留痕与预期一致，退出码 0 |

## 风险与取舍

| 风险 | 处理 |
|---|---|
| `alias_reject` 表被正常流量灌满 | 只记**被拒**的，正常声明不记（A 组测试直接断言 `count_pending==0`） |
| 留痕写失败反而阻断记忆写入 | `record_safe` 吞异常只告警 |
| `append_alias_edges` 返回值改类型 → 老调用方/老测试静默错 | 给了 `__eq__`/`__int__` 兼容；调用方只有 `alias_apply` 一处 |
| `label_by_rules` 产生进程级副作用（写 `_DECLARED_SURNAMES`） | 有意的：同句内立刻生效。`conftest` 加 autouse 夹具每个测试后 `reset_surnames()` |
| 姓氏声明跨 namespace 可见 | 已记录。姓氏本身不敏感，且 `labeler` 无 namespace 概念 |
| 2 字名字（`种明`）仍可能漏 | 接受。会进 `alias_reject`，看得见、能补 —— 这正是本 spec 的核心产出 |

## 相关文档

- [ADR-001 架构](../../docs/ADR-001-memory-architecture.md)
- [用户手册 · 别名判据](../../docs/user-manual.md)
- 上一个提交：`2fe762b` refactor(label): 判据统一成「单字只做位置检查，包含检查只用于多字词」

## 交付状态

> 补记：本 spec 是在代码已动工后才补建的（流程失误，已在会话中确认）。
> 下表如实标注此刻的真实状态。

| 落点 | 状态 |
|---|---|
| `_judge_name` / `NameVerdict` / 确定性 `_first_marker` | ✅ 已实现 |
| `extract_alias_claims_verbose` + `LabelResult.alias_rejects` | ✅ 已实现 |
| `alias_reject.py` 服务 + `record_safe` | ✅ 已实现 |
| `surname_decl.py` 服务 + `load_into_labeler` | ✅ 已实现 |
| `_MEASURE_SURNAMES` 豁免 + `SURNAME_DECL_RE` | ✅ 已实现 |
| `graph_outbox`：`AliasSkip` / `AliasWriteResult` / `_partition_alias_claims` | ✅ 已实现 |
| `remember`：两侧留痕 + 姓氏落库 | ✅ 已实现 |
| 两个入口启动加载姓氏 | ✅ 已实现 |
| admin 四个接口 | ✅ 已实现 |
| `tests/test_alias_reject.py`（41 条） | ✅ 绿 |
| `tests/test_admin_http.py` 新增 8 条 | ✅ 绿 |
| A11 全量回归 + `check_guide` + `e2e_mcp` | ✅ 789 passed / OK / OK，退出码全 0 |
| A12 真库活体验证 | ✅ 7/7 断言通过，退出码 0 |
| 文档（`user-manual.md` 三个新章节 + changelog v0.15） | ✅ 已写 |
| 提交 | ✅ 两笔（代码+测试 / 文档+spec） |

## 验收证据

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
  种师道 -> 老种 (label=exact, conf=1.0, eid=evt_1b2c08378a592364)

alias_reject（3 条）:
  side=alias     reason=ending   detail='的'    三哥主打的 -> 有声有色有味
  side=canonical reason=marker   detail='这种'   糖包 -> 这种人
  side=canonical reason=leading  detail='位'    小李 -> 位小明

surname_decl: ['种']

[PASS] 老种就是种师道 → 建边（种师道 过 nr 豁免）
[PASS] 左值被拒 → alias_reject 留痕 — ending/'的'
[PASS] 右值被拒 → alias_reject 留痕 — marker/'这种'
[PASS] 位小明 被拒（量词字不是姓氏，收窄生效） — leading/'位'
[PASS] 位小明 没建边
[PASS] 姓X 落库 — ['种']
[PASS] 正常声明不产生留痕噪声
OK: A12 真库活体验证全绿
```

## 实现过程中发现、值得记一笔的五件事

1. **`_DESCRIPTIVE_MARKERS` 是 `frozenset`，`detail` 会随机。**
   第一版直接 `for marker in _DESCRIPTIVE_MARKERS` 取第一个命中，
   测试里 `那个东西` 的 `detail` 每次不一样 —— 因为字符串哈希种子每个进程都变。
   加了 `_first_marker()` 固定排序键。
   **这不是测试挑剔，是落库值不确定**：同一句话两次记下不同理由，排查时会对不上。

2. **jieba 的 HMM 会把 `位小明` 标成 `nr`。**
   原设计「整串是单一 `nr` 词条就豁免」实测会把 `位小明` 放进来。
   所以加了第一道闸：量词字本身得是姓氏（`_MEASURE_SURNAMES`）。
   探针实测：`种师道`/`种小明`/`种砚舟`/`种氏` → `nr`；`种人` → `n`；
   `种老师`/`种先生` → 双 token；**`位小明` → 也是 `nr`**。

3. **`姓X` 的正则 `(?![\u4e00-\u9fa5])` 挡不住 `姓名`。**
   句末时「后面没有汉字」成立，`名` 照样被捕获。
   补了 `_NOT_SURNAME_CHARS = {"名","氏"}`。

4. **`A 就是 B` 的 `canonical` 是 B。** 判据检查 `canonical`，
   所以验证豁免必须让 `种师道` 落在**右边**（`老种就是种师道`）。
   第一版写成 `种师道就是老种`，边建出来了，但测的是干净词 `老种` ——
   **断言过了、豁免没验到**，靠 A12 第一轮 FAIL 才暴露。

5. **`_strip_prefix` 会先削掉 `那个`/`这个`/`我说的`。**
   `糖包就是那个ai中最弱智的` 到判据手里已经是 `ai中最弱智的`（7 字），
   撞的是长度上限而不是标记判据。

## 遗留

- `leanspec validate` 对 `status` / `priority` 报
  `value type does not match field kind` —— `001` 报同样的错，
  说明是工具对 enum 字段的校验问题，不是本 spec 的格式错。
- `alias_reject` 里 `resolution="accepted"` 目前**只标记、不补建边**。
  补建要重走写入链路（`event_id` 是内容哈希），归 P3。

