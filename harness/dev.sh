#!/usr/bin/env bash
# 开发常用命令（在 WSL 里执行；代码本身跑在 Windows 宿主）
#
#   ./harness/dev.sh up        启动中间件容器
#   ./harness/dev.sh down      停止容器
#   ./harness/dev.sh ps        查看容器状态
#   ./harness/dev.sh fixtures  重新生成合成夹具
#   ./harness/dev.sh test      跑后端测试
#   ./harness/dev.sh mcp       本地冒烟 MCP server
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$ROOT/docker-compose.yml"

case "${1:-}" in
  up)
    docker compose -f "$COMPOSE" up -d
    echo "等待 healthy ..."
    docker compose -f "$COMPOSE" ps
    ;;
  down)
    docker compose -f "$COMPOSE" down
    ;;
  ps)
    docker compose -f "$COMPOSE" ps
    ;;
  fixtures)
    ( cd "$ROOT/back_end" && uv run python ../harness/gen_fixtures.py )
    ;;
  test)
    ( cd "$ROOT/back_end" && uv run pytest -q )
    ;;
  mcp)
    ( cd "$ROOT/back_end" && MEMORY_NAMESPACE="${MEMORY_NAMESPACE:-agent-a}" \
        uv run memory-mcp <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
EOF
    )
    ;;
  *)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
