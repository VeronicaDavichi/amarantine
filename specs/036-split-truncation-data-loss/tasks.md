---
phase: tasks
---

# 036 任务：分片切分 + 可观测兜底

> 父 spec：[README.md](README.md) · 需求：[requirements.md](requirements.md) · 设计：[design.md](design.md)

## 任务清单

- [ ] **T1 复现脚本**
  - 用 10-30 语料拼出 joined，直接调 `split_events`，断言「原始条数 > 40 且截断后 == 40」
  - 引用：REQ-1（反证现状）
  - 验证：脚本输出 `raw=65 kept=40`，退出码 0

- [ ] **T2 `llm_split.py` 增 `split_all()`**
  - 按行递归二分 + `MAX_SPLIT_ROUNDS=3`，返回 `(parts, rounds)`
  - 引用：REQ-1、REQ-4
  - 验证：单测传入「假 LLM 每次返回 30 条」的 100 行文本 → 全部返回，`rounds >= 2`

- [ ] **T3 `raw_quote.py` 改用 `split_all`**
  - `ingest_batch` 调 `split_all` 替代 `split_events`，把 `dropped_events` / `split_rounds`
    写进 `IngestOutcome` 与 job
  - 引用：REQ-1、REQ-2、REQ-3、REQ-4
  - 验证：`/admin/ingest` 提交 ≤40 条批次 → job 里 `split_rounds=1`、`dropped_events=0`

- [ ] **T4 `ingest_job` 加列 + 响应字段**
  - `ALTER` 加 `dropped_events`（默认 0）、`split_rounds`（默认 1）；`Model` 与响应同步
  - 引用：REQ-3、REQ-4
  - 验证：老库启动不报错；`GET /admin/ingest/{job_id}` 两个字段都有值

- [ ] **T5 截断 WARNING 日志**
  - 到保护上限仍超限时打 WARNING，带 job_id / kept / dropped / rounds
  - 引用：REQ-2
  - 验证：`caplog` 断言日志出现，且 `dropped > 0`

- [ ] **T6 测试（见下方验收映射）**
  - 引用：全部 REQ
  - 验证：`pytest -q` 全绿，新增用例逐条对应 A1–A6

- [ ] **T7 文档 + 真库补灌**
  - `docs/user-manual.md` ingest 章节补「分片」与 `dropped_events`
  - 用 `--suffix r2` 重灌 10-29 / 10-30，验证 ESB 卡出现
  - 引用：REQ-1（端到端）
  - 验证：Weaviate 存在 summary 含「ESB」的卡；Neo4j 有对应 Event 节点

## 验收映射（需求 → 测试）

| 验收 | 覆盖 REQ | 手段 |
|---|---|---|
| A1 65 条输入全部落库、`dropped_events=0` | REQ-1 | 假 LLM 单测 |
| A2 ≤40 条输入 summaries 与改动前一致 | 不变行为 | 夹具对比 |
| A3 单行长文本走兜底、`dropped_events>0` 且有 WARNING | REQ-2 | 猴补 LLM 返回值 |
| A4 LLM 抛错仍返回 `[整段]`、job 不 failed | 不变行为 | 注入异常 |
| A5 同 `dedupe_key` 二次提交返回同一 job | 不变行为 | 接口测试 |
| A6 真库重灌后 ESB 卡与 Event 节点存在 | REQ-1、REQ-3 | 真库脚本 |

## 完成定义（DoD）

- [ ] A1–A6 全绿，全量 `pytest` 无回归
- [ ] `harness/check_guide.py` 仍 `OK`（配置项没漂移）
- [ ] `check_spec.py` 对本 spec 报 PASS（多文件模式）
- [ ] 真库重灌报告：补回条数 + ESB 卡片证据

## 进展记录

### 2026-11-06 · 手工重灌（不改代码的临时解法）

代码修复落地前，先用「每轮 ≤15 条」绕开 40 上限，验证「不丢」是可达的：

| 指标 | 旧（5 批大切片） | 新（18 批 ≤15 条） |
|---|---|---|
| 批次数 | 5 | 18 |
| 单批最大产出 | 40（撞上限） | **26** |
| `njzq-dingtalk` 卡片 | 189 | **229** |
| ESB 卡片 | 0 | **1** |

- A6 的「卡片存在」部分已达成：`任泽安请对方有空看一下群里ESB查询手机号的需求`
  （Event `evt_c86b9fb170193950`）
- **但代码层仍是截断逻辑**：换个批次大小就会复现，T2/T3 未做之前不能算修好
- 重灌前整体清空过 namespace（Weaviate 物理删 189、Neo4j `DETACH DELETE` 209、
  SQLite 删 176 原文 / 13 job），`work` 命名空间未受影响
- 过程中发现 SQLite 索引损坏（`wrong # of entries in index idx_raw_quote_session`），
  已 `REINDEX` + `VACUUM` 修复；根因是 `/mnt/e` 上 Windows 与 WSL 两侧同时写 WAL
- 遗留：229 张卡的 `date_start` 仍全是入库日 `2026-11-06`（与 036 无关的既有 bug）
