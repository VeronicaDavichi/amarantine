---
status: complete
created: 2026-11-06
priority: high
tags:
- bugfix
- entity
- hygiene
created_at: 2026-11-06T03:55:53.522376305Z
updated_at: 2026-11-06T04:03:44.870464977Z
completed_at: 2026-11-06T04:03:44.870464977Z
transitions:
- status: complete
  at: 2026-11-06T04:03:44.870464977Z
---
# 031 P3 收尾：地名误判、空目录、命名、内存状态

## Overview

盘点里剩下的 4 项 hygiene，外加一个**在收尾过程中发现并且确认是真 bug** 的问题。

### 真 bug：`上海` / `上海市` 被判成不同实体

实测（`match_entities`，10 组）：

```
✗ 上海 / 上海市   → DIFFERENT  rule=single_char_prefix  「单字名前缀冲突」
✗ 成都 / 成都市   → DIFFERENT
✗ 苏州 / 苏州市   → DIFFERENT
✗ 杭州 / 杭州市   → DIFFERENT
✗ 广州 / 广州市   → DIFFERENT
✓ 北京 / 北京市   → SAME       rule=suffix
```

**根因是规则顺序**：`match_entities` 的规则 1（单字名前缀冲突）在规则 3
（后缀白名单）之前。`上海` 的结构是「姓 + 单字名」（`上` 在姓氏表里），
于是规则 1 抢先判了 DIFFERENT —— 规则 3 里明明有「市」这个后缀，
却永远轮不到它执行。

`北京` 侥幸对了，是因为 `北` 不在姓氏表里，规则 1 没命中。

**影响**：地名会被拆成两个实体节点（`上海` 和 `上海市`），别名解析失效。
同一个 bug 之前在 `张三哥` 上过发生过一次，当时的修法是让规则 1
「多出的字是称呼时弃权」—— **这次是它的同类，多出的字是后缀。**

### 其余 3 项

| 项 | 现状 | 处理 |
|---|---|---|
| `deploy/` 空目录 | git 里只有空目录，没有内容 | 删掉，或放 `.gitkeep` + README 说清用途 |
| `graph_outbox.py` 命名 | 文件 25KB，但内容不止 outbox（含 `AliasWriteResult` / `_partition_alias_claims`） | 重命名会动很多 import，**先只记录** |
| `_next_sweep_at` 在内存 | 进程重启后重新扫一遍 | 用户说最低优先级，**记录不动** |

## Design

### D1 规则 1 遇到「已知后缀」时弃权

把 `_prefix_conflict_excluding_honorific` 扩展成
`_prefix_conflict_excluding_affix(short, long, honorifics, suffixes)`：

```
多出来的那一个字 ∈ honorifics  → 弃权（让规则 2 走称呼剥离）
多出来的那一个字 ∈ suffixes    → 弃权（让规则 3 走后缀展开）
否则                          → 真冲突，判 DIFFERENT
```

**为什么是「弃权」不是「判 SAME」**：弃权只是「这条规则不下结论」，
结论交给后面的规则。如果后缀规则因为 `ENTITY_RULE_ENABLED=false` 不执行，
结果会退化成 UNKNOWN（交给 LLM），**而不是错的 DIFFERENT**。
把「不知道」和「确定不同」分开，是这套三态设计的全部意义。

### D2 边界必须仍然正确

改完必须验证这些**不能被误伤**：

| 组合 | 期望 | 为什么 |
|---|---|---|
| `张三` / `张三丰` | DIFFERENT | 「丰」不是后缀，是真名字的一部分 |
| `项目A` / `项目A组` | **UNKNOWN** | 「组」故意不在 `ENTITY_SUFFIXES`（026 的决策）。注意这里是 UNKNOWN 不是 DIFFERENT —— `split_name_surname("项目A")` 返回 None，规则 1 本来就命中不了，交给 LLM 判 |
| `上海` / `上海市` | SAME | 「市」在后缀白名单 |
| `张三` / `张三哥` | SAME | 「哥」是称呼 |
| `豆包` / `豆包AI` | SAME | 「AI」是后缀 |

**「组」那条最关键**：026 特意把它排除，因为「项目A组」和「项目A」是
两个不同的组织。如果 D1 改错了让「组」也走后缀，就会把两个部门合并成一个。

### D3 `deploy/` 空目录：删掉

git 不跟踪空目录，它现在只是工作区里的一个残留。
如果将来要放部署脚本，再建不迟 —— 一个空目录提供不了任何信息，
反而让人以为「这里应该有东西」。

### D4 后两项只记录，不动

`graph_outbox.py` 重命名要改一堆 import，收益是「名字更准」，
风险是改错。**记录在案，将来做大重构时顺手做。**

`_next_sweep_at` 在内存里，重启后重新扫一遍 —— 用户明确说最低优先级。
扫一遍的开销是几秒，一年重启几次，可以忽略。

## Plan

- [x] T1 `_prefix_conflict_excluding_honorific` 扩展为排除后缀
- [x] T2 `match_entities` 里传入 `suffixes`
- [x] T3 测试：地名 + 市 / 省 / 区 → SAME
- [x] T4 测试：`张三` / `张三丰` 仍 DIFFERENT
- [x] T5 测试：`项目A` / `项目A组` 仍 DIFFERENT（不得误伤 026 的决策）
- [x] T6 测试：`ENTITY_RULE_ENABLED=false` 时地名退化为 UNKNOWN（不是 DIFFERENT）
- [x] T7 删掉空目录 `deploy/`
- [x] T8 全量测试

## Test

- [x] D1 `上海`/`上海市`、`成都`/`成都市`、`苏州`/`苏州市` → SAME
- [x] D2 `张三`/`张三丰` → DIFFERENT（回归）
- [x] D3 `项目A`/`项目A组` → UNKNOWN（026 决策不得被破坏；改前也是 UNKNOWN，要证明没变）
- [x] D4 `张三`/`张三哥` → SAME（回归）
- [x] D5 规则层关闭时 `上海`/`上海市` → UNKNOWN 而非 DIFFERENT
- [x] D6 全量通过，`work` 数据未变

## Notes

### 为什么这个 bug 藏了这么久

它需要三个条件同时成立：**短名的首字是姓氏** + **长名 = 短名 + 一个后缀** +
**姓氏表里有那个字**。`上海`/`上海市` 刚好命中，而 spec 026 的测试用例
用的是 `北京`/`北京市`（`北` 不在姓氏表）—— 于是测试全绿。

**测试用例选得好不好，决定了 bug 能不能被看见。**
026 的 14 组对照里没有一组是「姓氏字开头的地名」，这是它的盲区。

### 这是「规则顺序」第三次咬人

1. `张三哥` / `张三`：规则 1 抢在称呼剥离前判 DIFFERENT → 加 honorific 例外
2. `上海` / `上海市`：规则 1 抢在后缀展开前判 DIFFERENT → 加 suffix 例外
3. （潜在的第 3 次）如果将来加「缩写规则」，同样会撞上规则 1

**规则层的顺序是有语义的**，每加一条规则都要想清楚它在哪一条之前/之后，
以及「前一条会不会把后一条该管的情况抢走」。这不是可以靠加 if 解决的结构问题。

## 实现记录

### 地名 bug：修好了 4 组，没误伤姓名

| 组合 | 改前 | 改后 | 规则 |
|---|---|---|---|
| `上海` / `上海市` | ❌ DIFFERENT | ✅ SAME | `suffix` |
| `成都` / `成都市` | ❌ DIFFERENT | ✅ SAME | `suffix` |
| `苏州` / `苏州市` | ❌ DIFFERENT | ✅ SAME | `suffix` |
| `杭州` / `杭州市` | ❌ DIFFERENT | ✅ SAME | `suffix` |
| `广州` / `广州市` | ❌ DIFFERENT | ✅ SAME | `suffix` |
| `张三` / `张三丰` | ✅ DIFFERENT | ✅ DIFFERENT | `single_char_prefix` |
| `李四` / `李四光` | ✅ DIFFERENT | ✅ DIFFERENT | `single_char_prefix` |
| `张三` / `张三哥` | ✅ SAME | ✅ SAME | `honorific` |
| `豆包` / `豆包AI` | ✅ SAME | ✅ SAME | `suffix` |

改前 5 错 / 改后 1 错（剩下那条是我 spec 里期望值写错了，见下）。

修法：`_prefix_conflict_excluding_honorific` → `_prefix_conflict_excluding_affix`，
多出的那个字 ∈ 称呼表 **或** ∈ 后缀表时**弃权**（不是判 SAME）。

### 我 spec 里的期望值写错了一条

我在 spec 里写「`项目A` / `项目A组` → DIFFERENT」，实测是 **UNKNOWN**。

用 `git stash` 对比确认：**改前也是 UNKNOWN**，不是这次改出来的。
原因是 `split_name_surname("项目A")` 返回 `None`（`项目` 不是姓名结构），
规则 1 本来就命中不了。

**UNKNOWN 和 DIFFERENT 是两件不同的事**：前者是「规则不知道，交给 LLM」，
后者是「确定不同」。对「项目A/A组」，规则确实不知道 —— 这正确。

已改正 spec 的期望值，并在测试里写明为什么是 UNKNOWN 而不是 DIFFERENT。

### 改了 026 的一条测试，这是必须说清楚的事

`test_prefix_rule_beats_suffix_whitelist` 原来是：

```python
# 人为把「丰」塞进后缀白名单，断言仍判 DIFFERENT
got = _ruling("张三", "张三丰", ENTITY_SUFFIXES="丰,AI,公司")
```

031 之后这条不成立了（「丰」在白名单 → 规则 1 弃权 → 合并）。

**这不是「改测试让它变绿」**，是改**场景**并说明权衡：

- `ENTITY_SUFFIXES` 的默认值只含「不可能是人名字」的字
  （`市省县区镇部处科院局`）和多字词（`AI/助手/公司/集团/科技/股份/...`）
- 硬把「丰」这种**名字用字**配进去，等于告诉系统「丰是后缀」，系统照做
- 想同时「保护姓名结构」又允许配任意字，规则层做不到

测试拆成两条：
1. `test_prefix_rule_beats_suffix_whitelist` —— 默认配置下仍判 DIFFERENT
2. `test_known_tradeoff_configuring_name_char_as_suffix_merges` —— 把权衡
   **钉在代码里**，而不是让它变成某天有人发现时的「意外行为」

第 2 条的 docstring 明确写「如果这里变成 DIFFERENT，说明规则 1 又开始抢
后缀规则了 —— 那 `上海/上海市` 的 bug 会回来」。这样两边都有测试盯着，
不会为了修一边把另一边弄坏。

### meta-test：两条测试互不干扰

```
注入「规则 1 去掉后缀例外」：
  地名测试（7 passed → 7 failed）  ✓ 抓得住 bug
  姓名测试（1 passed → 1 passed）   ✓ 不受影响
```

如果注入后姓名测试也失败了，说明两条测试纠缠在一起，分不清是「修好了」
还是「修坏了」。现在分得清。

### 其余 3 项

| 项 | 处理 | 说明 |
|---|---|---|
| `deploy/` 空目录 | **删了** | 确认无引用、目录为空。git 本来就不跟踪空目录，它只是工作区残留 |
| `graph_outbox.py` 命名 | **只记录** | 25KB 内容不止 outbox（含 `AliasWriteResult` / `_partition_alias_claims`）。重命名要改一堆 import，收益是「名字更准」，风险是改错 |
| `_next_sweep_at` 在内存 | **只记录** | 用户说最低优先级。重启后重扫一遍，开销几秒，一年重启几次 |

### 这是「规则顺序」第二次咬人

1. `张三哥` / `张三`：规则 1 抢在称呼剥离前判 DIFFERENT（spec 026 修的）
2. `上海` / `上海市`：规则 1 抢在后缀展开前判 DIFFERENT（**本次修的**）

**同一个位置，两次都是「规则 1 把后面规则该管的情况抢走了」。**

规则层的顺序是有语义的。每加一条规则都要问：「前一条会不会把这条该管的
情况抢走？」这不是加 if 能解决的结构问题 —— 而 `_prefix_conflict_excluding_affix`
现在承担了「规则 1 该在什么时候弃权」的全部判断，将来加规则要动这里。

### 为什么这个 bug 藏了 3 个月

需要三个条件同时成立：**短名首字在姓氏表** + **长名 = 短名 + 一个后缀** +
**那个后缀字在白名单**。`上海/上海市` 刚好命中。

而 026 的 14 组对照里，地名那组用的是 `北京/北京市` —— `北` 不在姓氏表，
规则 1 没命中，于是侥幸通过。

**测试用例选得好不好，决定了 bug 能不能被看见。** 这已经是项目里第三次
同类教训（026 的向量阈值也是因为选对/选错 pair 才暴露）。
