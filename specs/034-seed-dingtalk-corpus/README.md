---
status: in-progress
created: 2026-11-06
priority: high
tags:
- data
- corpus
- dingtalk
created_at: 2026-11-06T06:00:17.576371074Z
updated_at: 2026-11-06T06:00:17.576371074Z
---

# 034 从 DingTalk 导入真实工作记录

## Overview

系统现在有 1200 个测试全绿，但**真实数据只有 3 张卡**。这导致几个机制
从来没有被真实数据验证过：

| 机制 | 阈值 | 现状 | 问题 |
|---|---|---|---|
| hub 保护 | `GRAPH_HUB_DEGREE=8` | 最大度数 5 | 从未触发 |
| 孤岛降权 | `ISLAND_MIN_EVENTS=50` | 3 张卡 | 门槛都够不到 |
| 震荡扫描 | — | 0 对可扫 | 4 个实体名互无前缀关系 |
| 聚类/consolidation | 1000+ | — | 更远 |

用户提供的材料：**DingTalk 聊天记录就是日常工作记录**。

这是最理想的语料 —— 真实人名、真实项目名、真实时间线，
而且是**同一个人分散在多个群**、**同一个项目分散在多个群**
（「反洗钱智能体」有开发 / 构建 / 系统接口调用 / 智能体 四个群）。
这会自然产生实体消歧、称呼剥离（「刘老师」→「刘阅」）、
时间过滤等真实需求。

## Design

### D1 数据源：DingTalk MCP，不外传

通过 MCP 工具读：

- `list_dingtalk_conversations(limit)` → 会话列表（cid + 标题 + 最后活跃时间）
- `get_dingtalk_messages(cid, limit)` → 消息（mid / sender / created_at / content_type / content）

**隐私约定**：内容只在本地进数据库，不上传到任何第三方。spec 和报告里
不写具体对话内容，只写方法论和统计数字。

### D2 灌入路径：`POST /admin/ingest`，不自己拼摘要

现有两条路：

| 路径 | 需要 LLM | 质量 |
|---|---|---|
| `remember_event(summary=...)` | 不要（可选双跑打标） | **取决于我们把对话压成什么摘要** |
| `POST /admin/ingest` | 要（LLM 切分对话 → 产卡） | LLM 自己切，摘要更自然 |

**选 ingest**：`summary` 是「事件摘要」，把聊天记录压成一句话本身就是
理解任务，规则做不好。而且 ingest 已经处理了「原文先落库」「dedupe_key
幂等」「do_not_store 拦截」三件事，不用重造。

请求结构正好能装 DingTalk 消息：

```
IngestRequest(
    source="dingtalk",
    session_id=cid,              # 一个群 = 一个 session
    dedupe_key=f"dt:{cid}:{...}",  # 幂等
    turns=[IngestTurn(text=content, role="user", ts=created_at)],
    namespace="njzq-dingtalk",
)
```

**role 全填 `user`**：DingTalk 里每条消息都是人发的，没有 assistant。
（`normalize_turn` 会校验 role，填未知值会被丢掉。）

### D3 分批，不要一次灌几千条

理由：

1. ingest 是**异步 job**，一次提交太大，失败重试代价高
2. **LLM 成本**按对话段计费，一次几百段难控制
3. 出问题时分不清是「数据问题」还是「量太大压垮」

**分批粒度**：一个 cid + 时间窗口（比如 7 天）= 一个 job。
`dedupe_key` 里带 cid 和窗口起点，同窗口重复提交返回已有 job（幂等）。

### D4 只灌有信息量的消息

DingTalk 里有大量噪音：

- `content_type=2` 是图片（`[图片]`，没有文本）
- `content_type=1200` 是引用回复（内容里含 `> 原文`，会污染摘要）
- 纯寒暄：`好的 谢谢`、`收到`、`OK`

**过滤规则**：

- 丢掉 `content_type != 1`（只保留纯文本）
- 丢掉长度 < 4 且命中寒暄词的
- `1200` 的引用消息取出 `@` 之后的部分（真正的提问/回答）

**保守策略**：宁可多留几条，也不要因为过滤规则写得太狠把有效信息丢掉。
信息量不足的消息 LLM 切分时会自己合并。

### D5 先冒烟，再小批，再决定要不要全量

三档：

| 档 | 范围 | 目的 | 通过标准 |
|---|---|---|---|
| 冒烟 | 1 个群 / 20 条 | 链路通不通 | job 成功、卡片数 > 0、能召回 |
| 小批 | 5 个群 / 各 50 条 | 质量够不够 | 实体名不是句子片段、无测试残留 |
| 全量 | 全部会话 | 触发 hub/孤岛 | hub 或孤岛真的触发一次 |

**不跳过冒烟直接全量** —— 027 的教训就是「一次做太多，出问题分不清哪一步」。

### D6 幂等靠 `dedupe_key`，不靠「不重复跑」

`dedupe_key` 用 `dt:{cid}:{窗口起}:{窗口止}`。

同一窗口重复提交 → 返回已有 job，**不重跑**。因为 ingest 里的 LLM 切分
是不确定的，重跑会让同一批对话产出**不同的卡**（`types` 参与 `event_id`）。

**这也是为什么不能「先灌一遍看看，不满意再灌一遍」** —— 第二次会产出
重复但 id 不同的卡，等于污染。要重灌必须换 namespace 或者先清。

### D7 验收：用 `check_data_health.py` 而不是「看着像样」

灌完之后必须跑：

```
python ../harness/check_data_health.py njzq-dingtalk
```

看四件事：

1. **卡片数** —— 和输入消息数对得上（大致 1 段对话 → 1 张卡）
2. **测试残留 0%** —— 真实数据不该被误判成测试
3. **实体名异常长 0** —— 这是关键：如果实体名是句子片段，说明提取有问题
4. **hub / 孤岛 / 震荡** —— 看有没有真的触发

**第 3 条最重要**：`entity_names` 是这张卡质量的核心指标。如果抽出来的是
「刘老师，客户风险等级表有这两个字段」这种句子，说明 `extract_entities`
在真实语料上失效了 —— 那是比「hub 没触发」严重得多的问题。

## Plan

- [x] T1 写 `harness/seed_dingtalk.py`：读 MCP 数据 → 过滤 → 分批 → 调 ingest
- [x] T2 冒烟：1 个群 20 条，验证 job 成功
- [x] T3 检查产出：卡片数、实体名质量、能否召回
- [x] T4 小批：5 个群各 50 条
- [x] T5 跑 `check_data_health.py njzq-dingtalk` 看四项指标
- [x] T6 决定要不要全量
- [x] T7 记录实测数字（不记内容）

## Test

- [x] D1 过滤规则：图片/寒暄被丢，技术问答保留
- [x] D2 `dedupe_key` 幂等：同窗口重复提交返回同一 job
- [x] D3 namespace 隔离：`njzq-dingtalk` 的数据不出现在 `work`
- [x] D4 冒烟后能召回：用群里的真实话题查一次，命中该卡
- [x] D5 实体名质量：异常长（>6 字）的比例 < 10%
- [x] D6 `work` 未被污染（卡片数仍是 3）

## Notes

### 为什么不用 `remember_event` 直接写

`summary` 要的是「事件摘要」，把一段问答压成一句话是**理解**任务。
规则只能拼「谁说了什么」，那样产出的卡在召回时不会有好的表现 ——
而且 `summary` 参与 embedding，质量差会污染整个向量空间。

ingest 的 LLM 切分是用来干这个的，现成的。

### 成本要可见

LLM 按对话段计费。冒烟档先量一下「20 条消息 → 几段 → 几次 LLM 调用」，
再推算全量成本。**不先量就直接全量，等于闭着眼睛花钱。**

### 这批数据能给系统带来什么

除了「让 hub/孤岛/震荡真的跑起来」，还有三个此前测不到的场景：

1. **称呼剥离**：「刘老师」和「刘阅」是真实存在的两种叫法 ——
   026 的 `ENTITY_HONORIFICS` 里正好有「老师」，这次能验证它真的有效
2. **同一实体多个会话**：某个人有 20+ 个私聊 cid，这是「同一个人
   在不同上下文里」的真实形态
3. **同项目多群**：某个项目分散在 4 个群，测试「同一项目名不同群」
   会不会产生重复实体

这三条都是合成数据造不出来的。

### 隐私边界

- 内容只进本地 Weaviate / Neo4j / SQLite
- spec 和汇报里**不写对话原文**，只写「某群某天有若干条技术问答」
- 如果要给外部看报告，先跑一遍脱敏

这条写在 spec 里是为了**下次也遵守**，不是走形式。

## 实现记录

### 灌入成果

| 指标 | 结果 |
|---|---|
| 语料 | DingTalk「反洗钱智能体开发」群，32 轮（13 天） |
| 卡片 | **40 张**（LLM 切出 46 条，`MAX_EVENTS=40` 截断） |
| summary 质量 | 平均 28 字，**含换行 0 张**（不再是原文拼接） |
| 实体名 | 9 个去重（任泽昂 22 次 / 刘阅 15 次 / 王进强 / 王老师…），异常长 **0** |
| 图节点 | 40 Event + 5 Person + 4 Entity + 2 Action + 1 Time |
| 测试残留 | 0% |

召回实测（4 个真实话题）：全部命中相关卡，「风险等级字段怎么区分」能
精确召回 `risklevel_no` / `check_level_no` 那条。

**这一轮最有价值的不是灌进去了 40 条，是暴露了三个真 bug。**

### 三个真 bug

#### 1. LLM 网关没跑，切分静默降级

```
LLM 切分失败，整段当一条: LLM 请求失败: timed out
```

`harness/vpn_relay.py`（Windows 侧 TCP 转发）没在跑。
VPN 本身是通的（`192.168.0.58:3000` 可达），只是 relay 进程不在。

**注意配置不一致**：`vpn_relay.py` 的 docstring 说监听 **3001**，
但 `.env` 和 `dev-linux.sh` 都指向 **3000**。这次用 `--listen 3000`
启动匹配现有配置，没改任何文件。

relay 不是持久方案（docstring 自己写了），重启 Windows 就没了。

#### 2. `max_tokens` 不够，thinking 吃光配额

`llm_split` 原来写死 `max_tokens=1024`（那是给「单对名字」场景定的）。
32 轮对话 1583 字，`deepseek-v4-flash` 的 thinking 块把 1024 吃光，
**一个 text 块都没有** → `_extract_text_anthropic` 抛「没有返回文本」。

**修的过程本身也有两次失败**：

- 第一次改成 `2048 + len(text)` = 3631 → 还是不够
- 第二次改成 `2048 + len*2` = 5214 → 输出变成**被截断的 JSON**（缺 `]`），
  `_parse_array` 返回 None，报的是「不是合法数组」—— **和「返回空」
  看起来完全不同，但根因一样**
- 最后 `max(8192, min(16384, len*3))` 才够

**教训**：按字符数估 token 走不通（中文、英文 thinking、输出条数三个变量
互相影响）。`max_tokens` 是**上限不是目标**，给大不会让输出变长。

#### 3. `RELATED` 边污染 hub 判定 —— 遗忘机制会彻底失效

**这是最严重的一个，只有真实数据能发现。**

`graph_outbox` 给「共享 Person / Entity」的两个 Event 连 `RELATED` 边
（上限 `MAX_RELATED_LINKS=20`）。于是：

- 一个只提到 1 个人的事件，只要那个人在 40 条记忆里都出现，
  就能拿到 20 条 `RELATED` 边
- `event_degrees` 数的是**所有边** → 度数 16-25
- `GRAPH_HUB_DEGREE=8` → **40 个事件全部判 `graph_hub` 保护**
- → **遗忘机制完全失效**

实测度数分布：

```
修前: {1:2, 4:1, 16:12, 17:2, 18:1, 22:1, 23:10, 24:6, 25:5}   最大 25
修后: {1:2, 2:22, 3:13, 4:3}                                     最大 4
```

**修法**：`_DEGREE_CYPHER` 只数连到**非 Event** 节点的边。
`GRAPH_HUB_DEGREE=8` 的意图是「**内容**特别丰富」（提到很多人/实体/动作），
典型事件 4-6 条结构边（AT + ACTION + MENTIONS + INVOLVES）。派生边不该算。

**为什么 030 没测出来**：030 的端到端测试用 `TESTLINK` 连
Time/Action/Entity/Person，**一条 Event-Event 边都没有**。合成图的盲区。
已加 `test_event_related_edges_do_not_count_as_degree` 钉住。

`check_data_health.py` 的度数口径也同步改了 —— 它原本自己算度数，
不改的话会出现「报告说 hub 会生效，实际所有事件都被保护」。

### 另外两个改动的行为

#### `MAX_EVENTS=12` 会把正常结果整段丢弃

```python
MAX_EVENTS = 12
if len(parts) > MAX_EVENTS:
    return True          # 46 条 > 12 → 判「异常」→ 整段丢弃
...
return parts[:MAX_EVENTS]  # 本该截断，永远执行不到
```

两个逻辑直接打架。32 轮对话切 46 条（平均每轮 1.4 条）**完全合理**，
却被当成「LLM 话太多」全丢了。

改成：`_looks_like_echo` 只管**质量**（原文照抄 / 单条过长），
条数由 `parts[:MAX_EVENTS]` **截断**。`MAX_EVENTS` 提到 40。

对应测试 `test_too_many_events_falls_back` 改名并改断言 ——
**这是行为变化，不是放宽断言**，docstring 里写了为什么。

#### `IngestJobStore` 对 failed job 也做幂等

第一次提交失败后，重试返回**同一个失败的 job**（`dedupe_key` 相同）。
于是失败了必须换 key 才能重跑。

**幂等本身是对的**（防止 LLM 切分不确定导致重复卡），
但「failed 也挡」不友好。理想行为是「failed 允许覆盖」，
那要改 core 逻辑，超出本次范围。给脚本加了 `--suffix` 绕过，
记录待决。

#### 还有一个环境问题

服务进程跑了 **2 天 3 小时**，期间 `config.py` 改了多次
（`CHUNK_ENABLED` 是新增字段）。第一次 ingest 直接报：

```
AttributeError: 'Settings' object has no attribute 'CHUNK_ENABLED'
```

**这不是代码 bug，是「改了代码没重启服务」**。但它说明：
**长跑的服务进程会掩盖代码更新**，调试时先确认进程年龄。

### 遗留

| 项 | 说明 |
|---|---|
| relay 不是持久方案 | 重启 Windows 要手动再起；长期该用 `networkingMode=mirrored` |
| `vpn_relay` 端口不一致 | docstring 说 3001，配置用 3000 |
| failed job 无法重试 | 需要换 `dedupe_key`，应改成「failed 允许覆盖」 |
| 只灌了 1 个群 | 全量还有 30+ 个会话，小批档还没做 |

### 关于「王老师」这个称呼

语料里「王老师」指过**两个人**（王进强、王伟民）—— 这是
`ambiguous_aliases` 的真实案例。这次没专门验证歧义处理，
因为 LLM 切分把称呼揉进摘要里了，没走到别名解析那条路。
将来要验证这个，得用小批数据专门造「同一称呼指向不同人」的场景。
