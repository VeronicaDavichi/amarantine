---
status: complete
created: 2026-11-03
priority: medium
tags:
- frontend
- admin
- m5
parent: '001'
created_at: 2026-11-03T02:51:04.073379362Z
updated_at: 2026-11-03T03:07:21.990286316Z
completed_at: 2026-11-03T03:07:21.990286316Z
transitions:
- status: in-progress
  at: 2026-11-03T02:59:29.693502858Z
- status: complete
  at: 2026-11-03T03:07:21.990286316Z
---
# 管理台前端（M5）：需求待澄清

## Overview

从 001 拆出来的 M5。**需求已拍板**，四个决策：

| 问题 | 答案 |
|---|---|
| 谁来用 | 单人（主人），但会有**多个 Agent** 并存 |
| 权限 | **不做登录/账号体系**；只校验 Bearer token |
| 域隔离 | 每个 Agent 一个 namespace，前端顶部**能看清、能切换** |
| 写权限 | **裁决 + 软删都可写**；物理删除仍然没有入口 |

需求依据的诚实账：ADR 全文没有「前端」章节，M5 只是 `P0-task-plan.md` 的一行。
所以本 spec 在扩接口时**逐条写明依据**，不假装 ADR 早就交代过。

**目标（二值判定）**

- B1 打开 `/ui` 能输入 token 并进入
- B2 顶部**始终可见**当前 namespace，切换后所有列表刷新
- B3 复核队列能逐条「接受」/「拒绝」，操作后该条从列表消失
- B4 误删率报告能读到 `buckets`（分桶计数）/ `sample`（抽检样本），且**只读**
- B5 事件列表能软删一张卡，删除前**必须在确认框里再写一遍域名**
- B6 能从一张卡的 `raw_quote_ids` 点进原文

**非目标**

| 不做 | 理由 |
|---|---|
| 登录 / 账号 / 角色 | 单人本机，账号体系是没有需求的复杂度 |
| 物理删除入口 | D7/A9：事实层永不物理删，前端也不给这个按钮 |
| 多用户并发编辑 | 单人使用，不做乐观锁 |
| 移动端适配 | 管理台在桌面上用；响应式留到真有人抱怨 |

**文档依据（docs/ 扫描）**

| 文件 | 用途 |
|---|---|
| `docs/P0-task-plan.md` M5 | 唯一的需求来源（一行：「前端能裁决与看报告」） |
| `docs/ADR-001-memory-architecture.md` A12 / A31 | 「看报告」「裁决生效」的接口级验收 |
| `docs/user-manual.md` | 25 个管理端点的 curl 用法 —— 前端只是把它们按钮化 |

## Design

### 技术形态：零构建单页（这是个有代价的取舍）

原生 JS + 单个 `index.html` + FastAPI `StaticFiles` 挂载在 `/ui`。

**为什么不上 Vue/React**：

| 约束 | 后果 |
|---|---|
| WSL 只有 15 GiB、明确要求别烧内存 | `npm install` 几百 MB + 构建进程 |
| 本机可能离线（VPN relay 是给 LLM 用的，不是 CDN） | CDN 版 Vue 一断网前端就全废 |
| 4 个页面、无路由、无复杂状态 | 框架的收益 < 依赖的代价 |

**代价**（写出来，不藏）：没有组件化、没有类型检查、状态管理是手写的一个对象。
如果以后交互真的复杂起来，迁移是**真成本**，不是零成本。届时再迁 Vue。

### 域切换是安全边界，不是 UX 装饰

这是本 spec 最重要的一条设计决定。

「不做登录」意味着**没有第二道防线**。可写（软删）意味着一次误操作能真删掉
别的 Agent 的记忆。所以：

1. 顶部**固定**显示当前 namespace，不靠滚动、不靠记忆
2. 切域后所有列表清空重拉 —— 不允许残留上一个域的行
3. 软删的确认框里**把域名再打一遍**，并要求用户手输一次才可确认

第 3 条不是仪式感：它让「在 work 域手滑删掉 personal 的卡」在物理上做不到 ——
人眼必须读到域名才能通过。

### 页面清单（4 个）

| 页面 | 数据源 | 写操作 |
|---|---|---|
| 复核队列 | `GET /admin/review?bucket=NEED_REVIEW` | `POST /admin/label-review/{event_id}/resolve` |
| 误删率报告 | `POST /admin/forget/sweep`（`dry_run=true`） | 无 |
| 事件列表 | **`GET /admin/events`（本 spec 新增）** | `DELETE /admin/event/{event_id}` |
| 溯源抽屉 | `GET /admin/raw/{quote_id}` | 无 |

### 为什么必须新增 `GET /admin/events`

「软删可写」的前提是**先能看见卡**。现有接口里只有 `/admin/review`
（仅复核队列）和 `/admin/entities/edits`（仅纠错痕迹），没有「列出这个域的卡」。

所以扩一个只读列表端点 —— 这是 ADR 没写的，理由记在这里而不是偷偷加：

```
GET /admin/events?namespace=&limit=50&offset=0
→ 200 {total, items:[{event_id, summary, date_start, types, importance,
                      pinned, is_deleted, raw_quote_ids, recall_count}]}
```

只读、分页、按 `is_deleted` 过滤掉已删的。**不提供**按内容搜索 —— 搜索走召回，
列表走分页，两件事不混。

### token 从哪来

单人、无登录：

- 页面首次打开要求粘贴 token，存 `localStorage`
- 每个请求带 `Authorization: Bearer <token>`
- token 错 → 401 → 页面显示「token 无效，请重填」

**风险（明说）**：`localStorage` 明文存 token，且 `/ui` 静态页本身不鉴权。
单人本机可接受；如果哪天要放到别人能访问的网络里，这个设计必须重做。

## Plan

- [x] T1 `GET /admin/events`：分页 + 域过滤 + 剔除已软删
- [x] T2 静态挂载：`StaticFiles` 到 `/ui`（HTML 无需 token，数据接口要）
- [x] T3 页面骨架：token 输入 + 顶部域切换 + 错误提示
- [x] T4 复核队列（只读列表 + 裁决可写）
- [x] T5 误删率报告（只读，`dry_run=true`）
- [x] T6 事件列表 + 软删（域名手输二次确认）
- [x] T7 溯源抽屉（点 `raw_quote_ids` 看原文）
- [x] T8 测试 D1–D7
- [x] T9 文档：user-manual 加「浏览器管理台」一节 + changelog v0.19

## Test

- [x] D1 `GET /admin/events` 返回分页结果，`total` 与 `items` 长度关系正确
- [x] D2 `/admin/events` 只返回当前域的卡，别的域一条不漏进
- [x] D3 `/admin/events` 不返回 `is_deleted=true` 的卡
- [x] D4 `GET /ui` 无需 token 即可拿到 HTML；`GET /admin/events` 无 token → 401
- [x] D5 前端域切换：切域后列表**完全替换**（不残留上一域的行）
- [x] D6 软删确认：未输入正确域名时**无法提交**（前端逻辑，用 JS 单测或断言 DOM 约束）
- [x] D7 溯源：卡的 `raw_quote_ids` 能换出 `GET /admin/raw/{id}` 的正文

## Notes

### 为什么把「域切换」写进 Test 而不是当 UX 细节

因为它承担了登录本该承担的那部分安全。没有登录的系统里，
「我当前在哪个域」是唯一防止误删的锚点 —— 锚点必须有验收。

### 为什么前端不给「物理删除」

`DELETE /admin/event` 的 `hard=true` 在 004 里已经被 **400** 关死了。
前端如果再提供一个「彻底删除」按钮，要么它调不动（假按钮），
要么以后有人为了让它能调而去改 004 的护栏。**不给按钮**比给了再拦更省事。

### 依赖关系

本 spec 是 001 的 M5 拆分项（`leanspec create --parent 001` 已挂父子关系）。
`leanspec link` / `rel` 在 0.3.0 里仍缺，所以跨 spec 的软依赖只能文字声明：
本 spec 的 T1 依赖 004 的 `DELETE /admin/event`、T7 依赖 005 的 `GET /admin/raw`。

### 实现记录

**1 · 零构建的代价被兑现了。**
`index.html` 一个文件 380 行，含 token 输入、域切换、4 个页面、溯源抽屉。
没有构建步骤，改完刷新即生效 —— 这对「单人管理台」正好。
代价也真实：没有组件复用，`renderReview` / `renderEvents` 里的表格 DOM 是手拼的，
将来第三个列表页出现时就该抽一层了（现在只有两个，抽早反而绕）。

**2 · 写前端时发现一个前后端契约差异（自审抓不到）。**
错 token 返回 **403**（`HTTPBearer` 的既有行为），不是 401。
我第一版只判 `res.status === 401` —— 于是「token 填错了」会显示成「加载失败」，
用户不知道该去重填 token。
修法：`401 || 403` 都走「请重填 token」，并补 `test_frontend_handles_both_401_and_403`。

**这条只能靠真跑发现** —— 单测里 `require_token` 的 401/403 是个常量，
不会有人去追问「前端接住了吗」。

**3 · D3 的实现方式与 spec 措辞不同（诚实记录）。**
spec 写「`/admin/events` 剔除已软删的卡」，实际实现是**不额外过滤** ——
因为 `WeaviateStore.list_all()` 的查询本身就带 `is_deleted` 过滤。
如果端点再过滤一遍，这个行为就被实现两次，将来改一处漏一处。
所以 D3 的验证走真库（造两张卡 → 软删一张 → 列表只剩一张），不在单测里造假。

**4 · D5/D6 用的是真浏览器，不是结构断言。**
单测里那两条 `index.html` 源码断言只能防「有人把二次确认删了」，
防不了「二次确认写错了」。所以另外用 playwright 真跑了一遍：

| 场景 | 结果 |
|---|---|
| 切域 `ui440d33` → `another-agent` | 行数 1 → 0，提示「这个域里还没有事件卡」 |
| 软删时输错域名 | 行数仍 1，toast「域名不一致或已取消 —— 没有删除」 |
| 软删点取消（`prompt` 返回 null） | 行数仍 1，同上 |
| 软删输对域名 | 行数 0，toast「已软删」 |

**5 · D7 的边界（没测到的部分）。**
溯源的后端链路（`GET /admin/raw` → `event_ids`）在 005 的 A11 已证过。
但前端「点原文按钮弹抽屉」这个交互**没有在真浏览器里点过** ——
测 D6 时把那张卡软删了，列表空了就没得点。
它是纯 `onclick` 跳转，风险低，但**没测就是没测**，记在这里。

### 验收证据

- 单测：`pytest tests/test_admin_ui.py -q` → **11 passed**
- 全量：`pytest tests -q --tb=line` → **861 passed**，零回归
- 真库（`ns=ui440d33`，真 Weaviate）：**6/6 全绿**
  - 造两张卡 → `total=2`
  - 软删 `evt_7718…` → 列表只剩 `evt_3dd6…`，`total=1`
  - 换域 `nope-not-exist` → `total=0`（域隔离成立）
- 真浏览器（playwright + uvicorn 起真服务，`127.0.0.1:8799`）：
  上表四个场景全部符合预期
- 守门：`check_spec.py`（6 个 spec）/ `check_guide.py` exit 0