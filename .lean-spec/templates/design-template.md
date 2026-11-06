# Design: {name}

> Related: [requirements.md](./requirements.md)

## Context

<!-- 当前状态、约束条件、与现有系统的关系 -->

## Goals / Non-Goals

**Goals:**
- [目标 1]
- [目标 2]

**Non-Goals:**
- [明确排除的内容]

## Architecture

### System Context

<!-- 该功能在系统中的位置，与外部依赖的关系 -->

### Component Design

<!-- 组件职责、接口定义、依赖关系 -->

## Data Models

<!-- 实体定义、字段说明、验证规则、关联关系 -->

## API Design

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| POST | /api/v1/... | ... | ... |

<!-- 请求/响应示例、错误码定义 -->

## Security Considerations

- [认证/授权方案]
- [数据保护]

## Error Handling

| Category | HTTP Status | Description |
|----------|-------------|-------------|
| Validation | 400 | 无效输入 |
| Authentication | 401 | 未认证 |
| Authorization | 403 | 无权限 |
| Not Found | 404 | 资源不存在 |

## Testing Strategy

- **Unit**: [覆盖范围]
- **Integration**: [集成点]
- **E2E**: [关键用户路径]

## Decisions & Trade-offs

| Decision | Rationale | Trade-off |
|----------|-----------|-----------|
| [决策] | [理由] | [取舍] |
