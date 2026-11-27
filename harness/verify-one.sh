#!/usr/bin/env bash
# verify-one.sh — 按单个 spec 跑验证点 + harness + e2e（/leanspec-complete 的入口）
#
# 用法：
#   ./harness/verify-one.sh 003-my-spec            # 实跑
#   ./harness/verify-one.sh 003-my-spec --dry-run  # 只列计划
#
# 退出码：0 全绿 / 1 有红 / 2 无可自动验证项或 spec 不唯一。
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$HARNESS_DIR/.." && pwd)"
cd "$ROOT_DIR"

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "FAIL: 找不到 python3 / python，无法跑 spec 验证" >&2
  exit 1
fi

exec "$PY" harness/verify_one.py "$@"
