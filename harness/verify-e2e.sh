#!/usr/bin/env bash
# verify-e2e.sh — 端到端（Playwright）验证入口。
#
# 本项目**未集成**该 capability：脚本存在，但只会回答「未集成」。
# 接口约定（/leanspec-propose 与 /leanspec-apply 依赖这个签名）：
#   ./harness/verify-e2e.sh [filter] headless
#
# 退出码：0 通过 / 1 失败 / 2 未集成。
# 严禁把 2 改成 0 —— 那会让「没做 E2E」在验收里变成绿灯，就是谎报。
set -euo pipefail

FILTER="${1:-}"

echo "UNINTEGRATED: 本项目还没有 E2E capability（Playwright）" >&2
if [ -n "$FILTER" ]; then
  echo "  请求过滤: $FILTER" >&2
fi
cat >&2 <<'EOF'
  补齐步骤：
    1) npm i -D @playwright/test && npx playwright install chromium
    2) 写 e2e/<req-slug>.spec.ts（断言用户可见行为）
    3) 把本脚本替换成真实实现，参考：ai-gateway-mcp/harness/verify-e2e.sh
  补齐之前：/leanspec-propose 生成的 UI 需求任务里，这条验证必须登记为
  「前置任务：集成 Playwright capability」，不能直接写成「跑绿」。
EOF
exit 2
