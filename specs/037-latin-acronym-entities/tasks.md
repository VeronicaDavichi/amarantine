---
phase: tasks
---

# 037 任务：英文缩写抽取通道

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 设计：[design.md](design.md)

## 任务清单

- [x] **T1 基线脚本**
  - 记录当前图上的 Entity 清单与英文节点数（应为 0），可复跑
  - 引用：REQ-1（反证现状）
  - 验证：脚本输出 `entities=11 english=0`，退出码 0

- [x] **T2 判据与停用词**
  - `labeler.py` 增 `_ACRONYM` / `_STOP_ACRONYMS` / `_looks_like_acronym`
  - 引用：REQ-2
  - 验证：单测对 `ESB`/`Kafka`/`MQ`/`PyCharm` 返回 True，对 `API`/`and`/`esb`/`123` 返回 False

- [x] **T3 接入第三条通道**
  - `_extract_entities` 中按「`_CODED_PATTERN` → 后缀扫描 → 缩写（跳过占位区间）」顺序
  - 引用：REQ-1、REQ-3、REQ-4
  - 验证：`项目A` 只出一条 topic；`extract_entities` 仍被查询侧复用（无新函数）

- [x] **T4 测试（见验收映射）**
  - 引用：全部 REQ
  - 验证：`pytest -q` 全绿，新增用例对应 A1–A6

- [x] **T5 真库验证**
  - 重灌含 ESB 的批次（依赖 036 的分片修复），确认节点与边
  - 引用：REQ-1、REQ-4
  - 验证：`MATCH (e:Event)-[:MENTIONS]->(x:Entity {name:"ESB"}) RETURN count(*)` > 0

- [x] **T6 文档**
  - `docs/user-manual.md` 抽实体一节补「英文缩写通道 + 停用词表」
  - 引用：REQ-2
  - 验证：`harness/check_guide.py` 输出 `OK: 文档与代码一致`

## 验收映射（需求 → 测试）

| 验收 | 覆盖 REQ | 手段 |
|---|---|---|
| A1 `ESB接口返回字段的数据字典` → 含 `ESB` | REQ-1 | 单测 |
| A2 `API网关限制了模型大小` → 不含 `API` | REQ-2 | 单测（停用词） |
| A3 `项目A 的系统日志` → `项目A` 一条、kind 仍 topic | REQ-3、不变行为 | 单测（去重） |
| A4 `PyCharm` / `Kafka` / `MQ` / `CIF` 各出 1 条 | REQ-2 | 参数化单测 |
| A5 `陈柏林询问…` → `陈柏林` | 不变行为 | 复用 035 用例 |
| A6 真库 `MENTIONS→ESB` 计数 > 0 | REQ-1、REQ-4 | 真库脚本 |

## 完成定义（DoD）

- [ ] A1–A6 全绿，全量 `pytest` 无回归（尤其 035 的名册用例）—— 目标模块 86 passed；全量为 1258 passed / 4 failed，那 4 条是本地 `.env` 开 `RERANK_ENABLED=true` 导致（去掉 .env 即全绿），与本 spec 无关
- [x] `harness/check_guide.py` 仍 `OK`
- [x] `check_spec.py` 对本 spec 报 PASS（多文件模式）
- [x] 真库报告：ESB 节点 + 至少一条 `MENTIONS` 边（ESB/Kafka 各 1 节点 1 边）

## 进展记录

### 2026-11-07 · 实现（T1–T6）

**与设计的两处偏离**（都是落地时才撞出来的）

1. **接入点不是 `_extract_entities`**：设计写的那个函数只在 **jieba 不可用的降级路径**
   里被调用；生产走 `_extract_with_jieba`。加在那儿等于「只对没装 jieba 的人生效」——
   正是本项目反复出现的「配置在、逻辑不在」。实际接在公共出口 `extract_entities()`，
   写入侧与查询侧共用（REQ-4）。
2. **判据多了两条**（真实语料逼出来的，500 张卡实测）：
   - **hex/UUID 片段必须挡**：`B8BB6E6F52`、`a4adda39`、`F439` 来自图片/文件 id，
     按原判据（含数字）会全部进图。
   - **名册人名跳过**：`Veronica Davichi` 是发言人，不能因为首字母大写额外建出
     `Veronica` 的 topic 节点。人名归 043 管。

**停用词表按真实语料扩了四组**：媒体后缀（jpg/png/jpeg/amr…）、URL/TLD（www/com/cn）、
XML 关键字（UTF/serviceID）、文件元数据字段（m_fileName）。原 spec 那 30 项只覆盖通用技术词。

**验收（真库）**

| 项 | 结果 |
|---|---|
| ESB | 节点 1 个、`MENTIONS` 边 1 条 |
| Kafka | 节点 1 个、`MENTIONS` 边 1 条 |
| 噪声型 Entity（后缀/hex） | **0 个** |
| 拉丁字母 Entity 总数 | 0 → **2**（就是 ESB、Kafka） |
| 实体总数 | 107 → 109（只多了这两个，没有连带噪声） |

基线脚本：`harness/entity_stats.py`（T1 要求「可复跑」，之前是临时敲的）。

**测试**：新增 `tests/test_acronym_entities.py` 23 条（正例 6 / 反例 13 / 去重 2 / 名册 2）。
反例里的 `jpg`、hex、`m_fileName` 全部取自真实摘要，不是编的。

**已知漏收（不改）**：全小写系统名（如 `dsh-music-insight`）收不到 —— spec §6 明确
「不收纯小写」，放开会让 `and`/`for` 涌入。已在测试里钉成「期望被拒」。
