# Spec 模板

本目录的模板是 **LeanSpec 官方版**（来自 `~/.dsh/skills/leanspec-propose/templates/`），
不是手搓的，改之前先确认上游。

## 用哪个

| 场景 | 模板 | 产出 |
|---|---|---|
| 单文件 spec（五段骨架） | `spec-template.md` | `README.md` |
| 缺陷修复（单文件，含「不变行为」） | `bugfix-template.md` | `README.md`（带 `type: bugfix`） |
| 需求驱动（三件套） | `requirements-template.md` + `design-template.md` + `tasks-template.md` | `requirements.md` / `design.md` / `tasks.md` |
| 设计驱动（先设计后需求） | `design-first-design-template.md` + `design-first-requirements-template.md` + `tasks-template.md` | 同上，顺序反推 |
| 设计驱动的入口说明 | `design-first-template.md` | 参考用 |

## 怎么用

**`--template` 必须给路径**，给模板名无效（CLI 0.3.0 实测）：

```bash
# ✅ 正确
leanspec create my-feature \
  --template .lean-spec/templates/requirements-template.md

# ❌ 无效（会回落到默认 spec-template）
leanspec create my-feature --template requirements-template
```

多文件 spec 的实际做法（CLI 的 `create` 只产出 README）：

```bash
leanspec create my-feature --template .lean-spec/templates/requirements-template.md
cd specs/NNN-my-feature
cp ../../.lean-spec/templates/design-template.md design.md
cp ../../.lean-spec/templates/tasks-template.md  tasks.md
# 再把 README.md 改写成薄入口（目标 + 导航 + 现状），指向这三份
```

## 占位符

创建时 CLI 会替换：

| 占位符 | 替换为 |
|---|---|
| `{name}` | 标题（kebab 名字转 Title Case） |
| `{date}` | 当天日期 |
| `{status}` | 初始状态（默认 planned） |
| `{priority}` | 优先级（默认 medium） |

## 与 `harness/check_spec.py` 的关系

校验器认两种形态：

| 形态 | 判定 | 要求 |
|---|---|---|
| 单文件 | 只有 `README.md` | frontmatter + 五段（Overview/Design/Plan/Test/Notes） |
| 多文件 | `requirements.md` + `design.md` + `tasks.md` 齐全 | README 薄入口（≥1 个二级标题 + 真实链接指向三份），三份各自非空且有二级标题 |

模板里的三件套带 `phase:` frontmatter 是官方约定，校验器不禁止（它只校验 README 的 frontmatter）。
**注意**：多文件模式下 README 必须是「薄入口」，不能照抄单文件模板的五段正文。

## 正文文件的 frontmatter 约定（我们的取舍）

官方模板给三件套带的是**完整 frontmatter**：

```yaml
---
status: planned
created: '{date}'
priority: medium
phase: requirements
---
```

我们只保留 `phase:` 一个字段：

```yaml
---
phase: requirements
---
```

理由：`status` / `created` / `priority` 的**唯一真相在 README**，
复制到正文会产生「README 说 in-progress、tasks.md 还写 planned」这类漂移。
`phase` 是正文文件独有的信息（它标记这是哪一份），保留它有意义。

现有 4 个多文件 spec（036 / 037 / 038 / 043）已按这个约定统一。

## 维护约定

- 上游更新时，用 skill 目录里的版本**整体覆盖**本目录，不要逐份改（易漂移）
- `spec-template.md` 是 CLI 内置名，覆盖它会影响 `--template` 不传时的默认行为 —— 改之前先想清楚
- 本目录不参与 spec 校验，但**会被 `leanspec templates list` 注册**，加文件等于加模板名
