#!/usr/bin/env bash
# verify-visual.sh — 原型还原的视觉对照（side-by-side）。
#
# 本项目**未集成**该 capability：脚本存在，但只会回答「未集成」。
# 接口约定（/leanspec-propose 的原型还原硬规则依赖它）：
#   ./harness/verify-visual.sh specs/NNN-<name>
# 依赖 per-spec 路由表 specs/NNN-<name>/assets/routes.json：
#   [{ "name": "<page>", "path": "/#/<route>" }, ...]
# 预期产物：specs/NNN-<name>/assets/visual-report.html（左原型右实现）
#
# 退出码：0 通过 / 1 失败 / 2 未集成。
# 严禁把 2 改成 0 —— 视觉没对照过就报绿，等于把「像不像」交给运气。
set -euo pipefail

SPEC_DIR="${1:-}"

echo "UNINTEGRATED: 本项目还没有 visual-regression/side-by-side capability" >&2
if [ -n "$SPEC_DIR" ]; then
  echo "  请求 spec: $SPEC_DIR" >&2
  if [ -f "$SPEC_DIR/assets/routes.json" ]; then
    echo "  routes.json: 已存在（$SPEC_DIR/assets/routes.json）" >&2
  else
    echo "  routes.json: 缺（先按 propose 的 T-V1 任务建这个文件）" >&2
  fi
fi
cat >&2 <<'EOF'
  补齐步骤：
    1) 归档原型截图到 specs/NNN-<name>/assets/prototype/（文件名 = 页面路由 name）
    2) 建 specs/NNN-<name>/assets/routes.json
    3) 集成截图 + 并排对比脚本，产出 assets/visual-report.html
  在补齐之前，视觉保真只能靠人工逐页对照并在 spec 里签字。
EOF
exit 2
