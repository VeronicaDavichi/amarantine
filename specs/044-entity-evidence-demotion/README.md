---
status: planned
created: 2026-11-07
priority: high
tags:
- entity
- evidence
- labeler
created_at: 2026-11-07T13:26:40.752953500Z
updated_at: 2026-11-07T13:26:40.752953500Z
---

# 实体证据与降级：先假设是 topic，按证据否决

> **Status**: planned · **Priority**: high · **Created**: 2026-11-07

## Overview

**要解决的问题**：拉丁词形该不该是 topic，现在靠**写入时猜**（静态停用词表），
而单个词条是不是「被谈论的主题」，单张卡根本看不出来。

**改成按证据判**：

```
① 写入：词形够就建 topic（先假设是）
② 累积：每见一次记一行证据（哪张卡、什么位置、有没有命名后缀、谁说的）
③ 定期：曝光量够 且 正向信号为 0 → 标 rejected，停止再建，软删已有边
④ 复活：之后出现命名上下文，或被查询命中 → 自动恢复
```

**实测依据**（037 上线后用 500 张真实 QQ 摘要跑一遍，新通道会新增 13 种 topic）：

| 词 | 出现卡数 | 位置 | 命名后缀 | 判断 |
|---|---|---|---|---|
| `Veronica` / `Davichi` | 27 / 27 | **全部在「X 说：」发言位** | 0 | 应否决（根因是导出语料丢了名册名） |
| `AI` / `GPU` | 2 / 1 | 陈述句中间，纯提及 | 0 | 应否决（通用词） |
| `Fm7` / `Cm7` | 1 / 1 | 音乐和弦 | 0 | 写入时按形态拒 |
| `IMF` / `LGBT` / `T80UD` / `NAS` / `AGI` / `ScalingLaw` | 各 1 | 提及 | 0 | **证据不足，不动** |

最后一行是核心约束：**证据不足 ≠ 证据相反**。只出现一次的词不能因为少就杀，
否则退化成「冷门词一律拒」，比静态停用词表更糟。

**为什么不在写入时一次判死**：单张卡看不到这个词在别的卡里怎么出现。
`IMF` 在这张卡里是提及，但它可能在十张卡里都是主题。只有跨卡累积才能分清
「顺嘴一提」和「反复讨论」。

## Design

### 证据表（SQLite）

```sql
CREATE TABLE entity_evidence (
    namespace   TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                  -- topic / person / ...
    mentions    INTEGER NOT NULL DEFAULT 0,     -- 出现次数
    cards       INTEGER NOT NULL DEFAULT 0,     -- 出现于多少张不同的卡
    suffix_hits INTEGER NOT NULL DEFAULT 0,     -- 与命名后缀共现（X接口/X系统）
    head_hits   INTEGER NOT NULL DEFAULT 0,     -- 出现于「X 说：」发言位
    queried     INTEGER NOT NULL DEFAULT 0,     -- 被召回查询命中
    status      TEXT NOT NULL DEFAULT 'active', -- active / rejected
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    reason      TEXT,
    PRIMARY KEY (namespace, name)
);
```

**为什么放 SQLite 而不是图属性**：图是派生层，`rebuild_graph` 一跑统计就没了；
热路径查 SQLite 也比查 Neo4j 便宜。抑制状态必须活在图的**外面**。

### 判定规则

```
rejected 当且仅当：
    cards >= DEMOTE_MIN_CARDS                    （曝光够，默认 5）
AND suffix_hits = 0                              （从未出现在命名上下文）
AND queried = 0                                  （从未被人问过）
AND head_hits >= cards * DEMOTE_HEAD_RATIO       （默认 0.8：基本只当发言人）

复活 当且仅当：
    suffix_hits > 0  或  queried > 0
```

`DEMOTE_MIN_CARDS` 是防误杀的核心旋钮：调到 0 就等于「冷门词一律杀」。

### 写入侧（与 037 的分工）

- 形态噪声**根本不进证据表**：媒体后缀、hex/UUID、音乐和弦在抽取时就拒
- 其余按词形建 topic，同时 upsert 一行证据（`mentions +1`，首次则建行）
- 建节点前查一次「是否 rejected」，命中即跳过

### 写图侧也必须过滤（关键）

`graph_outbox._split_entities(card)` 重放时用的是**卡上已存的 entities**，
不经过抽取器。抑制表只加在抽取层挡不住重放 —— 必须在写图那侧再过滤一次，
否则删了又长回来。

### 扫描

复用 033 的扫描族形态（`harness/meta_test_*.py`）：定期跑，输出
「本次降级 N 个 / 复活 M 个」报告，`--apply` 才落库。

## Plan

- [ ] **T1 证据表**：建表 + `upsert` / `query` 接口 + 老库自动建表
- [ ] **T2 写入侧接线**：抽到拉丁 topic 时记一行证据
- [ ] **T3 抑制过滤（抽取侧）**：建节点前查 rejected
- [ ] **T4 抑制过滤（写图侧）**：`graph_outbox` 写前过滤，堵住重放复活
- [ ] **T5 判定扫描**：`harness/entity_evidence_scan.py`，`--dry-run` 默认
- [ ] **T6 复活路径**：命名后缀共现或查询命中 → 置回 active
- [ ] **T7 管理接口**：`GET /admin/entities/evidence`（可查、可看 reason）
- [ ] **T8 测试与文档**：阈值边界、误杀防护、重放不复活、user-manual 补一节

## Test

| 验收 | 手段 |
|---|---|
| A1 曝光 5 张、零后缀、全发言位 → 判 rejected | 单测（固定数据） |
| A2 只出现 1 次 → **不动**（证据不足不杀） | 单测 |
| A3 rejected 后重新写入同一句 → 图里不新增该节点 | 真库 |
| A4 `pending_task` 重放 → 也不新增（写图侧过滤生效） | 单测 |
| A5 出现 `X接口` → 自动复活为 active | 单测 |
| A6 扫描输出「降级 N / 复活 M」，`--dry-run` 不改库 | 脚本 |

## Notes

### 非目标

| 不做 | 理由 |
|---|---|
| 用 LLM 判词义 | 规则层够用；LLM 已在摘要热路径，不该再加一次 |
| 图节点物理删除 | 沿用项目 tombstone 风格（`split_into` / `orphan_since`） |
| 跨 namespace 共享抑制 | 按 namespace 隔离：`AI` 在闲聊是噪声，在工作群可能是主题 |
| 自动清理证据表 | 一个词一行，体积可忽略，先留着 |

### 关联

- **037**：本 spec 消费它的词形通道；037 负责「建」，本 spec 负责「判」
- **033**：扫描器的工程形态直接复用
- **005 / 021**：软删与审计口径保持一致
- **043**：人名不该走 topic 通道，由 043 的账号准入解决，本 spec 兜底

### 风险

| 风险 | 缓解 |
|---|---|
| 阈值误杀冷门真词 | `DEMOTE_MIN_CARDS` 默认 5；被查询过即免疫 |
| 热路径多一次 SQLite 查 | 进程内缓存 rejected 集合，写路径只读缓存 |
| 抑制状态与图不同步 | 扫描只改 SQLite，图的边顺带软删，两者都可重跑 |
| 与 043 冲突（人名被当 topic 再判） | 043 落地后人名走 Person，本 spec 只兜剩余的 |
