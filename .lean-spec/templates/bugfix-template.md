---
status: planned
created: '{date}'
priority: high
phase: bugfix
type: bugfix
---

# Bugfix: {name}

> **Status**: {status} · **Priority**: {priority} · **Created**: {date}

## Bug Description

<!-- 简要描述 Bug 现象、影响范围、复现频率 -->

## Current Behavior

<!-- 用 WHEN/THEN 精确描述当前错误行为 -->

1. WHEN [触发条件] THEN system [当前错误行为]
2. WHEN [其他触发条件] THEN system [其他错误行为]

## Expected Behavior

<!-- 用 WHEN/THEN SHALL 描述期望的正确行为 -->

1. WHEN [触发条件] THEN system SHALL [期望正确行为]
2. WHEN [其他触发条件] THEN system SHALL [期望正确行为]

## Unchanged Behavior

<!-- 明确列出不应被修改影响的行为，防止回归 -->

1. WHEN [正常场景 1] THEN system SHALL CONTINUE TO [保持不变的行为]
2. WHEN [正常场景 2] THEN system SHALL CONTINUE TO [保持不变的行为]

## Root Cause Analysis

<!-- 根因分析：代码位置、逻辑缺陷、触发条件 -->

### Affected Files

| File | Issue |
|------|-------|
| `path/to/file` | [问题描述] |

### Root Cause

<!-- 根本原因的详细分析 -->

## Fix Design

<!-- 修复方案：最小化变更，只修必要的 -->

### Approach

<!-- 修复思路 -->

### Changes Required

| File | Change | Rationale |
|------|--------|-----------|
| `path/to/file` | [变更内容] | [原因] |

## Fix Tasks

- [ ] **Task 1**: [修复任务]
  - [具体步骤]
  - Test: 验证 Bug 已修复（Expected Behavior 通过）
  - Test: 验证 Unchanged Behavior 不受影响

- [ ] **Task 2**: [补充测试]
  - Test: Bug 复现测试（确认 Current Behavior 在修复前存在）
  - Test: 修复验证测试（Expected Behavior）
  - Test: 回归测试（Unchanged Behavior）

## Verification

- [ ] Bug 复现测试确认 Bug 存在
- [ ] 修复后 Expected Behavior 测试通过
- [ ] Unchanged Behavior 回归测试通过
- [ ] 无新增副作用
