---
status: complete
created: 2026-11-05
priority: medium
tags:
- sqlite
- config
- concurrency
- backend
created_at: 2026-11-05T09:10:14.758080397Z
updated_at: 2026-11-05T09:18:52.772707175Z
completed_at: 2026-11-05T09:18:52.772707175Z
transitions:
- status: in-progress
  at: 2026-11-05T09:10:15.644206199Z
- status: complete
  at: 2026-11-05T09:18:52.772707175Z
---
# 024 SQLite 并发参数接线（DB_BUSY_TIMEOUT_MS / SQLITE_WAL）

## Overview

用户要求「先保证功能实现」，这是当前零引用清单里**唯一影响运行时行为**的一对配置：

| 配置 | 现状 |
|---|---|
| `DB_BUSY_TIMEOUT_MS=5000` | **假实现** —— 没有任何代码读它 |
| `SQLITE_WAL=true` | **假实现** —— WAL 在 7 处硬编码开启 |

### 这个假实现为什么特别隐蔽（本 spec 的核心发现）

`recall_count.py:91` 写的是 `sqlite3.connect(path, timeout=5.0)`。

Python `sqlite3.connect()` 的 `timeout` 参数默认就是 `5.0` 秒，
对应 `busy_timeout = 5000 ms` —— **恰好等于配置默认值**。

于是：

- 观察 `PRAGMA busy_timeout` → 看到 5000，**看起来完全正确**
- 只有把配置改成 `10000` 再看，才会发现**它纹丝不动**

这是第 9 例「配了但没起作用」，也是**最隐蔽的一类**：
前 8 例靠「代码里有没有消费点」能抓到，这一例连观察行为都抓不到 ——
因为默认值和硬编码值相同。

**唯一能抓住它的测试形式是「改配置 → 行为必须变」。**
和 spec 023 里 `test_forget_sweep_days_drives_interval` 同一个道理。

### 顺带的不一致

11 处 `sqlite3.connect` 的 timeout 有两种值：

```
timeout=5.0   -> pending_task / raw_quote(x2) / recall_count /
                 directive_counter / embedding_guard / vocab_version   (6 处)
timeout=10    -> alias_reject / label_review / recall_trace / surname_decl  (5 处)
```

没有任何理由说明为什么分两组。这本身就是「没有统一入口」的证据。

## Design

### D1 统一连接工厂

新建 `memory/services/sqlite_conn.py`：

```python
def open_db(
    path: str | Path,
    settings: Settings | None = None,
) -> sqlite3.Connection:
    """建一个按配置设好并发参数的连接。"""
```

三步：
1. `sqlite3.connect(path, timeout=DB_BUSY_TIMEOUT_MS / 1000)`
2. `PRAGMA journal_mode=WAL`（`SQLITE_WAL` 为真时）
3. `PRAGMA busy_timeout` 显式设一遍（双保险：即使将来 `connect` 的
   `timeout` 语义变了，PRAGMA 是权威值）

### D2 `settings=None` 时保持现有行为

`open_db(path)` 不给 settings 时用**当前的硬编码值**（`timeout=5.0`），
这样：
- 未迁移的调用点行为不变
- 迁移可以分文件进行，不必一次性全改

但**最终目标**是全部走 settings —— `None` 分支只是迁移期的安全网。

### D3 两个不同的默认 timeout 怎么统一

现状 5.0 / 10 两组。统一到 settings 后，都变成 `DB_BUSY_TIMEOUT_MS / 1000 = 5.0`。

**这会改变那 5 个 `timeout=10` 的服务的等待时间（10s → 5s）。**

风险：`alias_reject` / `label_review` 这类是不是并发压力更大？如果是，
可能需要单独配置。但现在**没有任何证据**支持 10 秒是刻意的 —— 大概率是
不同时间写的不同人随手填的。

**决定**：统一到配置值，在 `## Notes` 记录这个行为变化。如果将来发现
某类表确实需要更长等待，再加一个按用途区分的配置，而不是保留魔法数字。

### D4 迁移范围

11 个文件。优先级：

| 优先 | 文件 | 理由 |
|---|---|---|
| P0 | `pending_task` / `recall_count` / `raw_quote` | 写频率最高，并发冲突最可能发生 |
| P1 | `directive_counter` / `embedding_guard` / `vocab_version` | 低频写 |
| P2 | `alias_reject` / `label_review` / `recall_trace` / `surname_decl` | 人工操作触发，几乎不并发 |

**全部迁移**，但按这个顺序做，中途可以停。

### D5 加一个「假配置检测」测试

本 spec 的真正产出不是那个 helper，是**能抓住这类 bug 的测试**：

```python
def test_busy_timeout_follows_config():
    """改配置 -> 实际 busy_timeout 必须跟着变。

    这是唯一能证明「不是假实现」的测试形式。
    观察默认值永远发现不了：5000 恰好等于 Python 的 timeout=5.0 默认值。
    """
```

同理给 `SQLITE_WAL` 一条：设 `False` → `journal_mode` 不能是 `wal`。

## Plan

- [x] T1 新建 `services/sqlite_conn.py`：`open_db()`（settings 可选）
- [x] T2 P0 三个服务迁移
- [x] T3 P1 三个服务迁移
- [x] T4 P2 四个服务迁移
- [x] T5 `test_busy_timeout_follows_config` —— 改配置行为变的证明
- [x] T6 `test_wal_follows_config` —— 同上
- [x] T7 `.env.example` / ADR 更新接线状态
- [x] T8 `test_config_usage.py` 白名单移除两项

## Test

- [x] D1 `DB_BUSY_TIMEOUT_MS=10000` → `PRAGMA busy_timeout` 是 10000（**这条不通就说明还是假实现**）
- [x] D2 `DB_BUSY_TIMEOUT_MS=1000` → 1000
- [x] D3 `SQLITE_WAL=False` → `journal_mode != wal`
- [x] D4 `SQLITE_WAL=True` → `journal_mode == wal`
- [x] D5 `open_db(path)` 不传 settings → 保持旧行为（timeout 5.0）
- [x] D6 迁移后的服务仍然工作（现有 1000+ 测试全绿）
- [x] D7 没有残留的裸 `sqlite3.connect`（静态检查）

## Notes

### 为什么这个比持久化优先

用户定「持久化最低优先级」。这个不同：

- 持久化只是「重启多扫一次」，**性能瑕疵**
- 这个是真配置不生效，用户改了不管用 —— **行为与文档不符**

后者更值得修，但它**不是紧急的**：现状 `timeout` 有 5~10 秒，
并发写不会立刻失败。所以修它是「让文档和行为一致」，不是「救火」。

### 一个必须记录的行为变化

统一后，5 个原本 `timeout=10` 的服务会变成 5 秒（`DB_BUSY_TIMEOUT_MS` 默认 5000）。

如果有人依赖那 10 秒，会感受到变化。但：
- 没有证据表明 10 是刻意的
- 现在这个值本来就不可能被配置影响，所以「10 秒」本身也不是承诺

### 这一例给「假实现检测」补上的新维度

已有的检测手段：
1. 零引用扫描（代码里有没有消费点）—— 抓不到这一例，`timeout=5.0` 是消费点
2. 观察运行时行为 —— **也抓不到**，因为默认值恰好等于硬编码值

新维度：**改配置 → 行为必须变**。这是唯一有效的形式，而且应该是
「每个配置项一条」的默认测试形式。

代价是每个配置都要写一条这样的测试。但比起「8 个配置里有 1 个是假的
且没人发现」，这个代价是值得的。

### 顺带记录：`GUIDE_CHECK_CRON` 不该接线

查这一类时确认过：它跑 `harness/check_guide.py`（仓库根目录脚本），
ADR 4.10 原文写「用户自行装 crontab」。所以它留在白名单是对的，
只是白名单理由要改准（见 spec 023 Notes）。

## 实现记录

### 发现：这一例的「假」特别隐蔽

`recall_count.py` 原来写的是 `sqlite3.connect(path, timeout=5.0)`。

Python `sqlite3.connect()` 的 `timeout` 参数**默认就是 5.0 秒**，对应
`busy_timeout = 5000 ms` —— **恰好等于 `DB_BUSY_TIMEOUT_MS` 的默认值**。

于是：

```
观察 PRAGMA busy_timeout                -> 5000   看起来完全正确
改成 DB_BUSY_TIMEOUT_MS=10000 再看       -> 5000   纹丝不动
```

以往往检测假实现的手段在这一例上**全部失效**：

| 手段 | 为什么失效 |
|---|---|
| 零引用扫描 | `timeout=5.0` 是消费点，扫不出来 |
| 观察运行时行为 | 默认值恰好正确，看到的是对的 |
| 读代码审查 | 能发现，但依赖有人正好看到那一行 |

11 处 `sqlite3.connect` 的 timeout 还分成两组（6 个 `5.0`、5 个 `10`），
没有任何理由说明为什么分——这本身就是「没有统一入口」的证据。

### 方案：统一到 `sqlite_conn.open_db()`

```python
conn = open_db(self._path)   # 并发参数由 DB_BUSY_TIMEOUT_MS 决定
```

一个关键设计：**不传 settings 时用进程级配置，不是硬编码默认值**。

如果 helper 在无 settings 时退回 `DEFAULT_TIMEOUT_S = 5.0`，那「配置不生效」
只是从 11 个服务搬到了 helper 里 —— **看起来改了，实际没改**。11 个服务都只
持有 `db_path`，不会传 settings，所以这条路径才是主路径。

进程级配置懒加载一次并缓存（配置要重启才变，每次开连接都构造 Settings 太贵）。

### meta-test：证明测试不是空转

写完 `test_busy_timeout_follows_config` 后，用 `harness/meta_test_busy.py`
验证了它真的能抓住 bug：

```
① 现状（配置生效）              -> 3 passed
② 把 helper 改回硬编码 timeout=5.0 -> 3 failed   ← 必须失败
③ 恢复                          -> 3 passed
```

**这一步不能省。** 一条「永远通过」的测试比没有测试更危险 ——
它会让人以为问题被覆盖了。特别是这一类默认值恰好正确的 bug，
测试很容易写成「断言默认值是 5000」，那就永远是绿的。

### 迁移结果

10 个文件（`raw_quote` 有 2 处 connect 但算 1 个文件），全部改完：

```
残留的裸 sqlite3.connect: 0 处
残留的 PRAGMA journal_mode: 只剩 sqlite_conn.py 里那 1 处
```

### 真库验证（三个场景，改环境变量）

```
── 默认 ──────────────────────────────────────────────
open_db(path) 不传 settings    busy_timeout=5000    journal_mode=wal
并发写实测等待                  5.01s

── DB_BUSY_TIMEOUT_MS=12000 ──────────────────────────
open_db(path) 不传 settings    busy_timeout=12000   journal_mode=wal
并发写实测等待                  12.01s      ← 连实际等待时间都变了

── SQLITE_WAL=false（新库）───────────────────────────
open_db(path) 不传 settings    busy_timeout=5000    journal_mode=delete
```

第三条是「改配置 → 行为变」最直接的证据：`wal` → `delete`。

### 一个必须记录的行为变化

统一后，5 个原本 `timeout=10` 的服务（`alias_reject` / `label_review` /
`recall_trace` / `surname_decl`）等待时间变成 **5 秒**（`DB_BUSY_TIMEOUT_MS`
默认 5000）。

判断它可以接受：
- 没有证据表明 `10` 是刻意的，大概率是不同时间写的不同人随手填的
- 现在这个值本来就不受配置影响，所以「10 秒」本身也不是对用户的承诺
- 如果将来发现某类表确实需要更长等待，应该加**按用途区分**的配置，
  而不是退回去留魔法数字

### `SQLITE_WAL` 的一个语义陷阱（已写进 `.env.example` 和 ADR）

`journal_mode` 是**库级持久属性**，写在库文件头里。所以：

- `SQLITE_WAL=false` 只对**新建**的库生效
- 已有库要显式 `PRAGMA journal_mode=DELETE` 才能退出 WAL
- `busy_timeout` 不同，它是**连接级**属性，每次连接都要设

这个区别不写清楚，会有人设了 `SQLITE_WAL=false` 却发现没变化。

### 测试

新增 9 条（`test_sqlite_conn.py`）：

| 测的什么 | 为什么重要 |
|---|---|
| **改 `DB_BUSY_TIMEOUT_MS` → busy_timeout 跟着变**（3 个值参数化） | 唯一能证明不是假实现的形式 |
| `SQLITE_WAL` 两个方向 | 同上 |
| `0` 是合法值（立即失败）不是「用默认」 | 边界 |
| 不传 settings 走进程配置**而不是硬编码** | 防止假实现搬家 |
| 真实并发：持锁时另一个连接**等**约 2 秒才失败 | busy_timeout 的实际意义 |

全量：**1080 passed**（1071 + 9）。

零引用：**15 → 13**。

### 给自己留的教训

从 spec 023 起，这类接线的验收标准固定成一条：

> **必须有测试证明「改了配置行为跟着变」，而不是「默认值看起来对」。**

代价是每个配置项要写一条这样的测试。但比起「13 个零引用里可能有几个是
这种假实现、而且永远不会被发现」，这个代价是值得的。

ADR 里新增了一节记录这个检测维度的演进。
