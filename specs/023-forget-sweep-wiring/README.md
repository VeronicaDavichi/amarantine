---
status: complete
created: 2026-11-05
priority: medium
tags:
- forget
- maintenance
- backend
- docs
created_at: 2026-11-05T08:58:24.805889390Z
updated_at: 2026-11-05T09:07:39.304748003Z
completed_at: 2026-11-05T09:07:39.304748003Z
transitions:
- status: in-progress
  at: 2026-11-05T08:58:25.816329104Z
- status: complete
  at: 2026-11-05T09:07:39.304748003Z
---
# 023 遗忘扫描接线 + idle_days=0 语义入 ADR

## Overview

两件独立但相关的事，都是「让遗忘策略真正可用」的收尾。

### 一、`FORGET_SWEEP_DAYS` 是零引用的假配置

`.env.example` 里写着 `FORGET_SWEEP_DAYS=30`（遗忘扫描周期），但**没有任何代码读它**。
扫描只能靠手动 `POST /admin/sweep`。

这是第 8 个「配了但没起作用」的配置，而且是最容易误导的一种 ——
用户看到「周期 30 天」会以为系统会自动跑，实际上永远不跑。

### 二、`FORGET_IDLE_DAYS=0` 的语义在 ADR 里没定义

实际行为（读代码得出）：`idle >= 0` 永远成立 → **立即淘汰所有满足其它三条件的卡**。

但它长得像「关闭」的哨兵值（`GRAPH_HUB_DEGREE=0` 就是关闭），
容易被误用。这个语义必须写进 ADR，不能只存在于代码的行为里。

## Design

### D1 接线位置：复用 `MaintenanceWorker`

现有 `MaintenanceWorker` 已经管理「到点才跑」的逻辑：

```
tick()        —— 同步跑一次，测试直接调
run_forever() —— 异步循环，30s 一次
_next_reconcile_at —— 到点才对账（现成的时间门槛模式）
```

加一个同构的 `_next_sweep_at` 分支即可，不引入新的调度器。

**为什么不用 cron / systemd timer**：状态在进程内，多一个外部依赖就多一个
「它没起来所以没扫」的排查面。而且 admin 服务本来就要常驻。

### D2 先做一次小重构：`_load_all` 提到 services 层

`_load_all` 现在在 `admin_http.py` 里（私有），它做三件事：
拉 Weaviate 全量 → 补 `RecallCounter` 的召回统计 → 转 `ForgetCandidate`。

`MaintenanceWorker` 在 services 层，不能用 admin 的私有函数。
所以提取到 `memory/services/forget_scan.py`：

```python
def load_candidates(settings, namespace) -> list[ForgetCandidate]
def scan(settings, namespace) -> ScanResult   # 加载 + 查图 + sweep 一步到位
```

admin_http 的两个端点改为调用它 —— **顺带消除 `/admin/review` 和
`/admin/sweep` 的重复逻辑**（之前 review 漏查图就是因为有两条独立的路径）。

### D3 结果：日志 + `TickReport` 字段

```python
@dataclass
class TickReport:
    retry: RetryReport
    reconcile: ReconcileReport | None = None
    sweep: ForgetSweepSummary | None = None   # 新增
```

`ForgetSweepSummary` 只装**汇总**，不装全部 verdicts（内存和日志都会爆）：

```
total / protected / need_review / safe_to_delete / graph_degraded
```

日志按桶打数量 + 抽样 event_id。

**不落库**（用户定的）。要做趋势分析时另开 spec —— 现在没有历史表，
`GET /admin/sweep` 每次实时算，这已经是现状。

### D4 启动时立即扫一次

与 `reconcile_on_start=True` 一致：进程启动后第一次 tick 就扫。

**风险**：频繁重启会频繁全量扫（Weaviate 拉全量是重操作）。
但扫描是只读的（`dry_run=True` 强制），代价只是 CPU/IO。
加 `sweep_on_start` 参数（默认 True），测试里关掉。

### D5 ADR 补充 `idle_days=0` 的语义

在 ADR 的配置表下面加一段说明：

```
`FORGET_IDLE_DAYS=0` 不是「关闭淘汰」的开关。实际行为是 idle>=0 恒成立，
即立即淘汰所有满足其它三条件的卡。要关闭淘汰请把值调大（如 99999）。
```

**不改代码**（用户定的）。只补文档，让现状可查。

## Plan

- [x] T1 新建 `services/forget_scan.py`：`load_candidates()` + `scan()`
- [x] T2 `admin_http` 两个端点改用 `forget_scan`（消除重复逻辑）
- [x] T3 `MaintenanceWorker` 加 `sweep_every_s` / `_next_sweep_at` / `sweep_on_start`
- [x] T4 `tick()` 到点跑 `scan()`，结果进 `TickReport.sweep`
- [x] T5 `TickReport.did_anything` 纳入 sweep
- [x] T6 `.env.example` 去掉「未接线」警告，改成真实说明
- [x] T7 ADR 补 `idle_days=0` 语义

## Test

- [x] D1 `FORGET_SWEEP_DAYS` 真的被读了（改值 → tick 间隔变）
- [x] D2 未到周期不扫（第二次连续 tick 不重复扫）
- [x] D3 到周期就扫（注入可控时钟）
- [x] D4 `sweep_on_start=True` 第一次 tick 就扫
- [x] D5 `sweep_on_start=False` 第一次不扫
- [x] D6 扫描抛异常不拖死循环（和 retry/reconcile 同样的容错）
- [x] D7 `TickReport.sweep` 装的是汇总，不含全量 verdicts
- [x] D8 `admin_http` 两个端点行为不变（回归）
- [x] D9 `idle_days=0` 的行为有测试钉住（现状：立即淘汰）
- [x] D10 全量 1060 条测试全绿

## Notes

### 为什么 `FORGET_SWEEP_DAYS` 是「天」而不是「小时」

遗忘是慢变量（默认 180 天才算久）。小时级精度没有意义，反而让
「上次扫描时间」的持久化更紧迫。天数足够。

### 与 spec 022 的关系

022 加了孤岛降权（默认关闭）。本 spec 让 `sweep` 定期自动跑 ——
如果将来开启孤岛，自动扫描会真的**加速淘汰**孤岛卡。

但 `dry_run=True` 强制仍在（A12 闸门未过），所以自动扫描只产出报告，
不会真的删。**这个约束不能在本 spec 里放松**。

### 一个还没解决的问题

`_next_sweep_at` 是内存变量，重启后丢失 → 重启就重扫一次。
`reconcile` 也是同样的设计（重启就对账），那里是对的（对账越快越好）。
对 sweep 来说，重启重扫的代价是「重复拉全量」，可接受但不理想。

真要解决需要把「上次扫描时间」落库，那属于 D3 里被否掉的持久化方案。
先记录，不实现。

### 顺带能消掉的重复

`/admin/review` 和 `/admin/sweep` 现在各有一份「拉卡片 + 查图」的代码。
D2 的重构会让它们共用 `forget_scan`，这类「两份逻辑不一致」的 bug
（review 漏查图就是这么来的）以后不会再出现。

## 实现记录

### T1 抽出 `services/forget_scan.py`

`_load_all` 原来在 `admin_http.py` 里（私有），`MaintenanceWorker` 在 services 层用不到它。
提到 `forget_scan.py` 提供三个东西：

| 函数/类 | 作用 |
|---|---|
| `load_candidates()` | 拉 Weaviate + 补 `RecallCounter` → `ForgetCandidate` |
| `scan()` | 加载 → 查图（度数 + 邻域）→ 分类，一步到位 |
| `ScanResult` | 报告 + 总数 + 图降级标记 |

`scan()` 接受**已加载的 rows**（`rows=` 参数），因为 admin 两个端点有自己的
`_load_all` 且**测试会 monkeypatch 它**（4 处）。如果端点改成内部自己加载，
那 4 处 monkeypatch 全部失效 —— 这不是「测试要改」，是「测试被绕过」。

### T2 合并两个端点的重复逻辑

`/admin/review` 和 `/admin/forget/sweep` 现在都走 `scan()`：

```python
# review
result = scan(cfg, ns, rows=_load_all(cfg, ns))
# sweep
result = scan(cfg, ns, rows=_load_all(cfg, ns), limit=body.limit)
```

**这是这次重构的真正收益**：spec 022 修的「review 漏查图导致两个端点分桶不一致」
就是两份实现造成的。现在只有一份，同类 bug 不会再出现。

### T3–T5 `MaintenanceWorker` 加扫描分支

复用现成的「到点才跑」模式（和 `_next_reconcile_at` 同构）：

```
_sweep_every_s  = FORGET_SWEEP_DAYS * 86400
_next_sweep_at  = None 表示「下次 tick 就扫」（sweep_on_start=True）
tick()          → 到点调 scan() → 结果进 TickReport.sweep
```

`ForgetSweepSummary` **只装汇总**，不装 verdicts（一次扫描可能几千张卡，
装进 TickReport 会让日志和内存都爆）。样例只留 5 条 event_id 供人工判断误删率。

`FORGET_SWEEP_DAYS <= 0` 时用兜底间隔 `30 天`，而不是禁用 ——
一个配错的周期不该让遗忘扫描永久不跑，那比配错更危险。

### T6–T7 文档

`.env.example` 里 `FORGET_SWEEP_DAYS` 的「⚠️ 当前未接线」警告换成了真实说明。

ADR 加了一节 **「配置值的「关闭」惯例不统一（已知陷阱）」**，因为查的时候发现
不止 `FORGET_IDLE_DAYS=0` 一个问题：

| 配置 | `0` 的含义 |
|---|---|
| `GRAPH_HUB_DEGREE` | 关闭这条硬信号 |
| `ISLAND_MAX_NEIGHBORS` | 关闭孤岛判定 |
| `FORGET_IDLE_DAYS` | **立即淘汰**（不是关闭） |
| `FORGET_MIN_RECALLS` | **关闭淘汰**（`recall_count < 0` 恒假） |

后两个方向完全相反，而且都没有文档。特别是 `FORGET_MIN_RECALLS=0`
实际上是个「永不淘汰」的开关 —— 这个之前完全没人提过。

### 真库验证

不 mock，走真实 Weaviate + Neo4j + SQLite：

```
FORGET_SWEEP_DAYS = 30  ->  扫描间隔 = 30 天

第一次 tick：共 3 张，保护 3 / 复核 0 / 可删 0，图降级=False
立刻再 tick：sweep=None  ✓ 没重复扫
跳过 31 天：又扫了一次    ✓ 满周期扫

判定：整条链路（配置 → 定时器 → 扫描 → 报告）是通的
```

3 张卡全 PROTECTED 符合预期 —— 重建时 `ingested_at` 是今天，都在宽限期内。

### 两个实施中发现的副作用

**① 三个 `MaintenanceWorker` 测试在偷着打网络**

`sweep_on_start` 默认 True，那 3 处实例化没关它，于是每次 tick 都会尝试
拉全量 Weaviate。**异常被 `_run_sweep` 吞掉了**（设计如此：一个 tick 失败不能
拖死循环），所以测试照样全绿，但耗时从 5.76s 变 6.78s。

如果只在「测试绿不绿」这个维度看，这个泄漏永远发现不了。
加了 `sweep_on_start=False` 后回到 5.76s。

**② 白名单条目过期被抓到**

`test_config_usage.py::test_whitelist_entries_are_still_unused` 失败了 ——
因为 `FORGET_SWEEP_DAYS` 一行还在零引用白名单里，而它已经接线了。

这正是那条测试存在的意义：白名单不是垃圾桶，条目一旦接了线就该删掉。
删掉后零引用从 **16 → 15**。

### 测试

新增 11 条（`test_forget_sweep_wiring.py`）：

| 测的什么 | 为什么重要 |
|---|---|
| `FORGET_SWEEP_DAYS` 改了间隔跟着变 | 唯一能证明「不是假配置」的测试 |
| `<=0` 用兜底而非禁用 | 配错不该让扫描永久不跑 |
| 到点才扫 / 未到点不扫 | 全量拉取是重操作 |
| `sweep_on_start` 两个方向 | 测试默认关，生产默认开 |
| 扫描失败不抛 + 仍推进下次时间 | 失败不拖死循环，也不每个 tick 重试 |
| 汇总不含 verdicts | 内存和日志的边界 |
| `did_anything` 忽略全 PROTECTED | 每天一条「0 个可删」是噪音 |
| **`idle_days=0` 是立即淘汰不是关闭** | 把语义钉住，改语义必须改测试和 ADR |

全量：**1071 passed**（1060 + 11）。

### 还没解决的问题（**最低优先级，接受现状**）

`_next_sweep_at` 是内存变量，**重启就重扫一次**。`reconcile` 也是同样设计
（那里是对的，对账越快越好），但扫描是重操作。

真要解决需要把「上次扫描时间」落库 —— 那属于 D3 里被否掉的持久化方案。

**决策（2026-11-05）：持久化定为最低优先级，不做。**

理由：
- 现状的代价只是「重启后多扫一次」—— 扫描是只读的（`dry_run=True` 强制），
  不产生错误状态，只是多花一次全量拉取的 CPU/IO
- 它是**性能瑕疵，不是功能缺陷**
- 把工时花在「能用的功能」上比花在「少扫一次」上更有价值

要重开这个话题的门槛：扫描变成可写（`dry_run=False` 落地）时，必须一起做。
那时「重启重扫」就不只是浪费，而是会把同一批卡重复处理 —— 性质变了。

### 顺带记录：`GUIDE_CHECK_CRON` 不该接线

查 `FORGET_SWEEP_DAYS` 时顺手看了同类的 `GUIDE_CHECK_CRON`，结论相反。

它跑的是 `harness/check_guide.py` —— 一个**仓库根目录的独立脚本**，不是包内模块。
ADR 4.10 原文也写着「**用户自行装 crontab**」。

所以它留在零引用白名单里是**正确的**，不该在服务里接线：
- 服务里起子进程跑仓库脚本 = 路径依赖 + 部署形态耦合
- 它属于开发/CI 工具，不是运行时功能
- 装 cron 是用户环境的事

但白名单里的理由写得不准（原来是「没有定时器」），应该改成
「按 ADR 4.10 由用户自行装 crontab，不属于服务运行时」。
