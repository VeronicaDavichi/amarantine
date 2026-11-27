#!/usr/bin/env bash
# verify-spec.sh — spec 骨架校验入口（/leanspec-apply 收尾必跑）
#
# 用法：
#   ./harness/verify-spec.sh          # 校验 specs/ 下所有 spec
#   ./harness/verify-spec.sh --help   # 参数透传给 check_spec.py
#
# 空项目（specs/ 下 0 个 spec）返回 0，并说明「骨架校验留到第一个 spec 之后」。
# 退出码：0 合规 / 1 有漂移。
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
  echo "FAIL: 找不到 python3 / python，无法校验 spec 骨架" >&2
  exit 1
fi

exec "$PY" harness/check_spec.py --allow-empty "$@"
