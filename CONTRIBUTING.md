# 参与开发

先读 [`AGENTS.md`](AGENTS.md) —— 它规定了本仓库的**开工流程**（先看板 → 再 spec → 才写码），
不是可选的建议。

## 环境

```bash
# 依赖（四个容器）
docker compose up -d

# 后端
cd back_end
uv sync --frozen
cp .env.example .env          # 至少改 ADMIN_TOKEN / LLM_BASE_URL / LLM_API_KEY
uv run pytest -q              # 期望全绿
```

冷启动细节见 [`docs/quickstart.md`](docs/quickstart.md)。

## 提交前必须跑

```bash
# 一条命令跑完静态部分（不需要容器）
bash harness/verify_all.sh --static

# 等价于
cd back_end && uv run pytest -q
python3 harness/check_spec.py       # 49 个 spec 的骨架与行数上限
python3 harness/check_guide.py      # 文档提到的配置项/工具必须真实存在
python3 harness/check_env_coverage.py   # .env.example 与 Settings 完全对齐
python3 harness/scrub_privacy.py        # 不得有真实姓名/内网 IP/QQ 号
```

`--static` 之外的 `verify_all.sh` 还会跑真库验收（`verify_046/047`），
需要容器与 LLM 网关。

## 改动规格（spec）

```bash
leanspec create <name> --title "..."      # 新建（会自动编号）
leanspec update <n> -s in-progress        # 开工前
leanspec update <n> -s complete           # 收尾前勾完 checklist
```

规矩：

1. **先有 spec 再动手**（Bug 修复与纯重命名可以例外）
2. 状态按实际推进：`planned → in-progress → complete`
3. **不做的事要写进 spec**，写清「为什么不做」——否则下一个人会再试一次
4. 每个 spec ≤400 行；超了就把过程记录压缩（`check_spec.py` 会拦）

## 代码约定

- **注释解释「为什么」，不解释「是什么」**。踩过的坑必须留字，例如：
  `# 踩过：用 budget-elapsed 时主召回常把预算用完，remain=-90ms`
- 外部依赖失败要**降级并告警**，不要抛给上层（见 `docs/degradation.md`）
- 幂等：任何写库/写图脚本都要能重跑（`MERGE` / `INSERT OR REPLACE` / 固定 UUID）
- 派生层（图）可以重建；**事实层（原文/卡片）不能丢**

## 隐私红线

本仓库**不内置任何真实语料**。`data/`、`corpus/`、`backups/`、
`docs/labeling-data.js`、`.env`、`harness/.privacy-map.json` 全部 gitignored。

提交前自查：

```bash
git log --all --name-only --diff-filter=A -- "*corpus*" "*data/*" "*.db" ".env"
```

有输出就要停下来 —— 历史里进了真实数据，只能重写历史或换干净仓库。

## Pull Request

- 一个 PR 一个主题；附「怎么验证」（最好贴命令与真实输出）
- 改判据/阈值时，在 PR 描述里给**改前改后**的数字对比
- UI 改动附截图；纯文档改动不用

## 报告问题

功能缺陷请附：复现步骤、`/health` 输出、相关日志片段（**先脱敏**）。
安全问题不要开 public issue，见 [`SECURITY.md`](SECURITY.md)。
