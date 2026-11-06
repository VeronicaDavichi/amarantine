---
phase: design
---

# 043 设计：账号数据怎么变成身份

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 任务：[tasks.md](tasks.md)

## 1. 实测数据源（先看有什么，再设计）

在 `C:\Users\EDY\.dingtalk_mcp\decrypted\dingtalk.db` 上实测：

| 表 | 行数 | 有用字段 | 结论 |
|---|---|---|---|
| `tbuser_profile_v2` | **469** | `uid`, `nick`, `alias` | **主数据源**；`alias` 全空 |
| `tbuser_alias_name` | 1 | `uid`, `alias`, `pinyin` | 备注格式样本：`王晓虹 - 加班问题人事` |
| `contact_friend` | **0** | `remark`, `alias` | 表在、数据空 → **备注拿不到** |
| `tbconversation` | 90 | `alias` | 会话别名（群名），与人无关 |

关键发现：

- **备注（remark）当前不可得** —— `contact_friend` 是空表。要么本地库没同步，要么钉钉把它存在服务端。
  这是本 spec 的**前置采集问题**，不能假装有。
- 两字昵称 **150/469 = 32%**，短名字不是边缘情况。
- 带 `·` 或英文的昵称 12 个（`DING助手`/`AI助理`/`Gavin`/`BOB洪亚军`），
  其中没有真正的新疆姓名样本，但说明「非汉族姓名」类别真实存在。

## 2. 优先级矩阵

| 优先级 | 来源 | 例 | 冲突时 |
|---|---|---|---|
| 1 | `uid` | `4690364584` | 身份的**锚**，不参与取名 |
| 2 | 备注（`tbuser_alias_name.alias` / `contact_friend.remark`） | `王晓虹 - 加班问题人事` | 拆出姓名部分作为主名 |
| 3 | 昵称（`tbuser_profile_v2.nick`） | `蓝网-韩晓迪` | 去掉「公司-」前缀后入库 |
| 4 | 通讯录真名（当前空） | — | 有则优先于昵称 |
| 5 | jieba 词形 | `周大福` | **只有在 1–4 全空时才用**，且名册非空时降级为 topic |

## 3. 备注解析（REQ-2）

```
王晓虹 - 加班问题人事
   ↓ 按「 - 」「 — 」「（」「(」切
姓名 = 王晓虹        → 入名册（Person）
说明 = 加班问题人事   → 入 Entity(topic) 或丢弃（不冒充人）
```

规则：
- 分隔符集合：` - `、`—`、`（`、`(`、`/`
- 姓名部分必须通过 035 的 `_is_clean_name`，否则整条不入名册
- 说明部分长度 > 12 时丢弃（大概率是句子）

## 4. 短名字（REQ-3）

**现在为什么能行**：035 之后，`person` 只认名册，而名册按**精确匹配**，与长度无关。
`王泳`（2 字）和 `王晓虹`（3 字）在名册里同等有效。

**要注意的是**：
- `MIN_PERSON_LEN=2` 不能放宽到 1（单字会误伤大量常用字）
- 两字名与常用词撞形（`石敏` vs `石敏…`）靠名册精确匹配解决，不靠长度

## 5. uid 锚定（REQ-4 / REQ-5）

```
(:Account {uid, platform:"dingtalk"})
    -[:OPERATED_BY {label:"exact", source:"account_table"}]->
(:Person {name:"王泳"})
```

- **同名不同 uid**（3 个「智小安业务助手」）→ 3 个 `Account`，1 个或多个人格节点（由 042 决定）
- **同 uid 改名** → `Account.name_history = [{nick:"旧名", until:"…"}]`，旧名进名册但不新建 `Person`

## 6. 非汉族姓名（REQ-6，暂缓）

检出条件（满足任一）：
- 含 `·`（如 `阿卜杜拉·买买提`）
- 长度 ≥ 5 且首字不在 `_COMMON_SURNAMES`
- 含非汉字字符

处理：**不建 `Person`**，记 `(:Entity {name, kind:"topic", non_han:true})`，
并写一行「未识别姓名」报告（供将来扩展）。**本次不解析**，只保证不误判。

## 7. 与其它 spec 的关系

| spec | 关系 |
|---|---|
| 035 名册准入 | 本 spec 扩展它的数据源，不改它的准入原则 |
| 041 指称层 | 权威 `Sense` 的提供者；`uid → Person` 就是一条 exact Sense |
| 042 账号族 | `Account` 节点由本 spec 生产，归并策略由 042 决定 |

## 8. 风险

| 风险 | 缓解 |
|---|---|
| 备注为空 → REQ-2 无数据可测 | 用 `tbuser_alias_name` 那 1 条 + 构造夹具；采集问题单列 T1 |
| 昵称带公司前缀（`蓝网-韩晓迪`） | 剥离规则，且保留原名做别名 |
| 名册过大（469）影响匹配性能 | 最长匹配先按首字分桶 |
| 改名导致历史卡片人名对不上 | 旧名保留在名册 + `name_history` |
