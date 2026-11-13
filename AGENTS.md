# amarantine

## 🚨 CRITICAL: Before ANY Task

**STOP and check these first:**

1. **Discover context** → Use `board` tool to see project state
2. **Search for related work** → Use `search` tool before creating new specs
3. **Never create files manually** → Always use `create` tool for new specs

> **Why?** Skipping discovery creates duplicate work. Manual file creation breaks LeanSpec tooling.

## 🔧 Managing Specs

### MCP Tools (Preferred) with CLI Fallback

| Action         | MCP Tool   | CLI Fallback                                   |
| -------------- | ---------- | ---------------------------------------------- |
| Project status | `board`    | `lean-spec board`                              |
| List specs     | `list`     | `lean-spec list`                               |
| Search specs   | `search`   | `lean-spec search "query"`                     |
| View spec      | `view`     | `lean-spec view <spec>`                        |
| Create spec    | `create`   | `lean-spec create <name>`                      |
| Update spec    | `update`   | `lean-spec update <spec> --status <status>`    |
| Link specs     | `link`     | `lean-spec link <spec> --depends-on <other>`   |
| Unlink specs   | `unlink`   | `lean-spec unlink <spec> --depends-on <other>` |
| Dependencies   | `deps`     | `lean-spec deps <spec>`                        |
| Token count    | `tokens`   | `lean-spec tokens <spec>`                      |
| Validate specs | `validate` | `lean-spec validate`                           |

## ⚠️ Core Rules

| Rule                                | Details                                                                                                               |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **NEVER edit frontmatter manually** | Use `update`, `link`, `unlink` for: `status`, `priority`, `tags`, `assignee`, `transitions`, timestamps, `depends_on` |
| **ALWAYS link spec references**     | Content mentions another spec → `lean-spec link <spec> --depends-on <other>`                                          |
| **Track status transitions**        | `planned` → `in-progress` (before coding) → `complete` (after done)                                                   |
| **Keep specs current**              | Document progress, decisions, and learnings as work happens. Obsolete specs mislead both humans and AI                |
| **No nested code blocks**           | Use indentation instead                                                                                               |

### 🚫 Common Mistakes

| ❌ Don't                             | ✅ Do Instead                                |
| ----------------------------------- | ------------------------------------------- |
| Create spec files manually          | Use `create` tool                           |
| Skip discovery                      | Run `board` and `search` first              |
| Leave status as "planned"           | Update to `in-progress` before coding       |
| Edit frontmatter manually           | Use `update` tool                           |
| Complete spec without documentation | Document progress, prompts, learnings first |

## 📋 SDD Workflow

```
BEFORE: board → search → check existing specs
DURING: update status to in-progress → code → document decisions → link dependencies
AFTER:  document completion → update status to complete
```

**Status tracks implementation, NOT spec writing.**

## Spec Dependencies

Use `depends_on` to express blocking relationships between specs:
- **`depends_on`** = True blocker, work order matters, directional (A depends on B)

Link dependencies when one spec builds on another:
```bash
lean-spec link <spec> --depends-on <other-spec>
```

## When to Use Specs

| ✅ Write spec        | ❌ Skip spec                |
| ------------------- | -------------------------- |
| Multi-part features | Bug fixes                  |
| Breaking changes    | Trivial changes            |
| Design decisions    | Self-explanatory refactors |

## Token Thresholds

| Tokens      | Status               |
| ----------- | -------------------- |
| <2,000      | ✅ Optimal            |
| 2,000-3,500 | ✅ Good               |
| 3,500-5,000 | ⚠️ Consider splitting |
| >5,000      | 🔴 Must split         |

## Quality Validation

Before completing work, validate spec quality:
```bash
lean-spec validate              # Check structure and quality
lean-spec validate --check-deps # Verify dependency alignment
```

Validation checks:
- Missing required sections
- Excessive length (>400 lines)
- Content/frontmatter dependency misalignment
- Invalid frontmatter fields

## First Principles (Priority Order)

1. **Context Economy** - <2,000 tokens optimal, >3,500 needs splitting
2. **Signal-to-Noise** - Every word must inform a decision
3. **Intent Over Implementation** - Capture why, let how emerge
4. **Bridge the Gap** - Both human and AI must understand
5. **Progressive Disclosure** - Add complexity only when pain is felt

---

**Remember:** LeanSpec tracks what you're building. Keep specs in sync with your work!


---

## 【验证】约定（2026-11-12）

**以 `【验证】` 开头的问题 = 必须去数据库确认，不能靠读代码推断。**

### 数据源

| 库 | 地址 | 用途 |
|---|---|---|
| Weaviate | `http://127.0.0.1:8081`（class `MemoryEvent`） | 卡片、时间、摘要、session |
| Neo4j | HTTP `http://127.0.0.1:7474/db/neo4j/tx/commit` | 图节点与边 |
| SQLite | `back_end/data/memory.db` | 原文、job、别名拒绝 |

### 回答必须包含

1. **查询顺序**：编号列出每一步（第 1 步、第 2 步…），每步给**实际命令/查询原文**
2. **每步的真实返回**（行数 + 关键字段；大结果抽样并说明抽样方式）
3. **术语来源标注**：每个检索词标明是「实体名 / 原话锚点 / 推测同义词」
4. **结论**：数据是否支持某判断

### 为什么要查询顺序

只给最终结论和一堆结果，用户无法判断中间有没有 LLM 幻觉（例如「先有结论，
再挑几条支持结论的结果」）。**编号的查询顺序是可审计的**：每步都写清
「用什么词查的、为什么用这个词、返回了几条」，用户能重放整条链路。

### 禁用

- 用**记忆卡片的摘要措辞**反过来当检索词（循环论证：摘要由原文生成，
  拿摘要的词搜原文只能证明模型措辞，不能证明原文）
- 只给「综合后的结论」，不给中间步骤
- 把「推测的同义词」混进结果里不标注

### 禁止

- 用「代码里是这么写的」代替数据库证据
- 库连不上时给推测 — **必须直说不可用**，并给出重连命令
- 把「我记得/上次是」当验证结果

### 环境检查顺序

```bash
docker ps --format '{{.Names}} {{.Status}}' | grep amarantine   # 容器
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/v1/.well-known/ready
```

---

## 时间口径（2026-11-13）

**对用户报时间一律用 UTC+8（北京时间 / QQ 显示口径），需要时再附 UTC。**

### 为什么

`qq-mcp` 返回的 `iso` / `timestamp` 是 **UTC**，而 QQ 客户端显示的是 **UTC+8**：

```
工具 timestamp 1793696889 → toISOString 2026-11-03T09:08:09Z
QQ 里同一条显示             2026-11-03 17:08
```

实测把 UTC 直接当本地时间报，会让用户看到的时间**整体早 8 小时**。

### 规则

- 报「几点/哪天」→ 先 `+8h`，写 `2026-11-03 17:08`
- 需要精确时附 UTC：`2026-11-03 17:08（UTC 09:08）`
- 本地服务日志（`admin.log`）本来就是 +08，直接用
- 涉及 epoch 的时间窗参数仍用 **epoch 秒**（与时区无关）

### 检查

```python
local = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
```
