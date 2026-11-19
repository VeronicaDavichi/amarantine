---
status: planned
created: 2026-11-19
priority: high
tags:
- release
- oss
- packaging
created_at: 2026-11-19T02:48:57.237087765Z
updated_at: 2026-11-19T02:48:57.237087765Z
---

# 049 开源发布准备（Open-source release readiness）

## Overview

目标：把 amarantine 从「能自己跑」推进到**可以公开开源发布的第一版**。

当前状态（2026-11-19）：

- 功能侧：48 个 spec 全部 complete；测试 **1603 passed**
- 交付侧：**没有**一键部署、没有备份恢复、没有版本号、没有 LICENSE 之外的合规材料
- 运维侧：86 个配置项无分组说明；无 CI；健康检查分散在 4 个服务各自探活

用户已决定：**开源发布**。发布前仍有若干工程细节由用户亲自处理，
本 spec 负责把「发布需要哪些门」列清楚并逐项验收。

## Design

### 发布门（Release gates）

### G1 可安装（别人能跑起来）

- [ ] `docker compose up` 一条命令起全套（weaviate + neo4j + embedding + rerank + admin）
- [ ] `GET /health` 汇总四依赖状态（返回各服务 ok/degraded 与原因）
- [ ] 首次启动自动建表/建 collection，无需手工脚本
- [ ] `.env.example` 覆盖所有**必填**项，并标注哪些可留空
- [ ] 冷启动文档：从 clone 到第一次 `memory_remember` 成功，全程可复现

### G2 数据安全（不丢、可迁）

- [ ] 备份脚本：SQLite `memory.db` + Weaviate 数据卷 + Neo4j dump，一次打包
- [ ] 恢复脚本：从备份还原，且**校验**还原后条数一致
- [ ] 版本迁移：SQLite schema 版本号 + 迁移脚本（现有库能升上来）
- [ ] 文档写明「三处状态必须一起备份」，否则会出现图与卡不一致

### G3 可运维

- [ ] 配置清单：86 项按用途分组（LLM / 存储 / 召回 / 抽取 / 闸门 / 调试），
      每项写「默认值 / 什么时候该改 / 改了会怎样」
- [ ] 结构化日志（JSON 行）或至少统一前缀，便于 grep 与采集
- [ ] 错误路径清单：LLM 不可用 / 向量库不可用 / 图库不可用 三种降级行为写清楚

### G4 可协作（开源合规）

- [ ] LICENSE 确认（当前仓库已有？待核）+ 第三方依赖许可证清单
      - nbnhhsh（Apache-2.0）已在 README 标注
      - bge-m3 / bge-reranker-v2-m3 的许可证与再分发条件需确认
- [ ] README 重写：这是什么 / 解决什么问题 / 30 秒快速开始 / 架构图 / 已知限制
- [ ] CONTRIBUTING.md（开发环境、测试、spec 流程）
- [ ] SECURITY.md（如何报漏洞；明确「自托管、单租户」的安全边界）
- [ ] 隐私说明：本项目会处理个人聊天记录，写清**不内置任何数据集**、
      `data/` 与 `corpus/` 永不入库（`.gitignore` 已覆盖，需在文档里说明）

### G5 可验证（可持续）

- [ ] GitHub Actions：pytest + check_spec + check_guide
- [ ] CHANGELOG.md + `v0.1.0` tag
- [ ] 发布前最后一遍：`bash harness/verify_all.sh --static` 全绿

## Plan

- [ ] P1 G1 可安装：compose 全套 + `/health` 汇总 + 首启自建表 + `.env.example` + 冷启动文档
- [ ] P2 G2 数据安全：备份 / 恢复 / schema 版本迁移 / 三处状态一起备份的说明
- [ ] P3 G3 可运维：86 项配置分组说明 + 结构化日志 + 三种降级路径写清
- [ ] P4 G4 合规：LICENSE 与依赖许可确认 + README 重写 + CONTRIBUTING + SECURITY + 隐私说明
- [ ] P5 G5 可验证：CI（pytest/check_spec/check_guide）+ CHANGELOG + `v0.1.0` tag

## Test

发布前的验收（都能二值判定）：

| 编号 | 验收 | 手段 |
|---|---|---|
| A1 | 干净环境下 `docker compose up` 后 `/health` 四项全 ok | 新容器跑一遍 |
| A2 | 冷启动按文档能完成一次 `memory_remember` → `memory_recall` | 照文档手做一遍 |
| A3 | 备份 → 清空 → 恢复后条数与恢复前一致 | 脚本比对 SQLite/Weaviate/Neo4j 计数 |
| A4 | 旧库能通过迁移脚本升级，`check_spec`/pytest 全绿 | 用一份旧备份跑 |
| A5 | `git log --all -- '*corpus*' '*data*'` 无真实语料 | 命令输出为空 |
| A6 | CI 在 push 上跑通 pytest + 两个 check 脚本 | GitHub Actions 记录 |
| A7 | `bash harness/verify_all.sh --static` 全绿 | 发布前最后一遍 |

## 用户自己处理的（不阻塞本 spec，但发布前需确认）

用户 2026-11-19：「我还有一些工程上的细节要处理」——具体内容未知，
本 spec 不假设；发布门 G4 的 LICENSE/依赖许可确认需要用户拍板。

## 风险

| 风险 | 缓解 |
|---|---|
| 仓库历史里混入真实聊天数据 | 发布前跑一遍 `git log --all -- '*corpus*' '*data*'`；必要时重写历史或新建干净仓库 |
| 依赖外部 LLM 网关才有完整功能 | README 明确「无 LLM 时降级为原文卡」；给一个本地 ollama 示例配置 |
| 单机单租户，被误认为多用户服务 | SECURITY/README 明确隔离粒度是 namespace，无鉴权分级 |
| 中文人名/群聊特性依赖过强 | 文档写明适用范围（中文群聊优先），英文语料效果未验证 |

## Notes

- 关联：040（导入工具，发布后用户自备语料）、044（抑制表，已 complete）
- 顺序建议：G1 → G2 → G3 → G4 → G5（先能跑，再能保数据，最后合规）
