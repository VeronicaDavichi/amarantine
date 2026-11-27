# harness/ — 验证与运行脚本的固定入口

本目录由 `/leanspec-init` 生成。**脚本名就是接口**：`/leanspec-propose`、
`/leanspec-apply`、`/leanspec-complete` 会按下面的名字去调用，不要改名。

| 脚本 | 用途 | 接口 | 退出码 |
|---|---|---|---|
| `verify-one.sh` | **单 spec 出口门禁**：验证点 + 命令 + harness + e2e | `./harness/verify-one.sh <spec> [--dry-run] [--fail-fast]` | 0 全绿 / 1 有红或阻塞 / 2 无可自动验证 |
| `verify_one.py` | 上者的实现（可单跑） | `python3 harness/verify_one.py <spec>` | 同上 |
| `verify-spec.sh` | 全仓 spec 骨架校验 | `./harness/verify-spec.sh` | 0 合规 / 1 有漂移 |
| `check_spec.py` | 上者的实现，可单跑 | `python3 harness/check_spec.py [--allow-empty]` | 0 / 1 |
| `verify-e2e.sh` | 端到端（Playwright） | `./harness/verify-e2e.sh [filter] headless` | 0 / 1 / **2 未集成** |
| `verify-visual.sh` | 原型视觉对照 | `./harness/verify-visual.sh specs/NNN-<name>` | 0 / 1 / **2 未集成** |
| `env.sh` | 变量出口（只放路径，不放密钥） | `source harness/env.sh` | — |

## 可执行验证点的唯一约定

spec 的 markdown 里，**fenced code block 内以 `$ ` 开头的行** = 可执行验证命令：

````markdown
## Test

- [ ] A1 召回 hit@5 不低于 0.55

```bash
$ ./harness/eval_recall.py --seed 7
```
````

`verify_one.py` 只认这个形态（不会去猜普通散文里哪句是命令）。
没有 `$ ` 行 = 只能人工验证 → 脚本返回 exit 2，**不是通过**。

## 当前状态：两个 capability 尚未集成

`verify-e2e.sh` 与 `verify-visual.sh` 是**桩**，只会 `exit 2` 并打印补齐步骤。
这不是偷懒，是**诚实**：

- `/leanspec-propose` 对每条 UI 需求要求「E2E 跑绿」，对带原型的 spec 要求「视觉对照」。
- `/leanspec-complete` 把 `exit 2` 当**阻塞项**（有 UI 需求／有原型时）——修不了就不许标 complete。
- 谁把 `exit 2` 改成 `exit 0`，谁就是在验收里造假。

## 换成本项目实现时

1. 保留文件名与参数签名（调用方只认名字）
2. 退出码语义不变：0 通过 / 1 失败 / 2 未集成
3. 在 `env.sh` 里加**路径类**变量；密钥走环境变量或 `.env`（且 `.env` 必须 gitignore）
4. 参考真实实现：`ai-gateway-mcp/harness/verify-e2e.sh`、`ai-gateway-mcp/harness/verify-visual.sh`

## 环境差异（Windows + WSL2）

- 这些脚本是 bash 脚本，在 WSL 里跑：`wsl bash -lc "cd /mnt/e/<repo> && ./harness/verify-one.sh 001"`
- Windows 侧没有 bash 时，直接跑 Python：`python harness/verify_one.py 001`
- 不要在 Windows 上把脚本存成 CRLF —— bash 会报 `$'\r': command not found`
