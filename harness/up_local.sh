#!/usr/bin/env bash
# 一键起本地全套（WSL 重启后跑这个）。
#
#   weaviate + neo4j   → docker compose
#   embedding          → harness/embed_server.py（GPU fp16，等价 TEI 契约）
#   rerank             → harness/rerank_server.py（GPU fp16，等价 TEI /rerank）
#   admin HTTP         → memory.entrypoints.admin_http（:8765）
#
# 为什么不用 docker compose 起这两个模型服务：这台机器拉 TEI 的 1GB+ 层反复超时，
# 而 bge-m3 / bge-reranker-v2-m3 用 GPU 跑 fp16 更省事也更快
# （embedding 加载 ~10s；rerank 50 条候选 ~40ms）。
#
# 用法：
#   bash harness/up_local.sh
#   VENV=/path/to/venv bash harness/up_local.sh     # 换 venv
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$HOME/.venv-amarantine}"

# 端口/地址从 .env 里取（服务端和客户端读的是同一个 *_BASE_URL）
_env_val() {
  local key="$1" default="$2" val=""
  if [ -f "$ROOT/back_end/.env" ]; then
    val="$(grep -E "^${key}=" "$ROOT/back_end/.env" 2>/dev/null | tail -1 | cut -d= -f2-)"
  fi
  printf '%s' "${val:-$default}"
}
EMBED_URL="$(_env_val EMBEDDING_BASE_URL http://127.0.0.1:8082)"
RERANK_URL="$(_env_val RERANK_BASE_URL http://127.0.0.1:8083)"
ADMIN_PORT_NUM="$(_env_val ADMIN_PORT 8765)"

if [ ! -x "$VENV/bin/python" ]; then
  echo "✗ 找不到 venv: $VENV（先 cd back_end && uv sync）" >&2
  exit 1
fi

DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
  else
    echo "✗ docker 不可用（当前用户不在 docker 组）" >&2
    exit 1
  fi
fi

echo "== 中间件 =="
$DOCKER compose -f "$ROOT/docker-compose.yml" up -d weaviate neo4j

echo -n "== 等 weaviate "
for _ in $(seq 1 30); do
  if curl -sf -m 2 http://127.0.0.1:8081/v1/.well-known/ready >/dev/null; then
    echo " ready"; break
  fi
  echo -n "."; sleep 2
done

echo "== embedding (GPU) =="
pkill -f "embed_server.py" 2>/dev/null || true
# 端口/是否预加载/key 全部来自 back_end/.env，不再传命令行参数
nohup "$VENV/bin/python" "$ROOT/harness/embed_server.py" > /tmp/embed.log 2>&1 &
for _ in $(seq 1 40); do
  if curl -sf -m 2 "${EMBED_URL}/health" >/dev/null; then break; fi
  sleep 2
done

echo "== rerank (GPU) =="
pkill -f "rerank_server.py" 2>/dev/null || true
nohup "$VENV/bin/python" "$ROOT/harness/rerank_server.py" > /tmp/rerank.log 2>&1 &
for _ in $(seq 1 40); do
  if curl -sf -m 2 "${RERANK_URL}/health" >/dev/null; then break; fi
  sleep 2
done

echo "== admin http =="
pkill -f "memory.entrypoints.admin_http" 2>/dev/null || true
( cd "$ROOT/back_end" && nohup "$VENV/bin/python" -m memory.entrypoints.admin_http \
    > /tmp/admin.log 2>&1 & )

sleep 6
echo
echo "== 体检 =="

# 模型加载要 10-20s，体检时可能还没就绪 —— 给它一个短等待，别把「还在加载」报成「未起」
wait_health() {
  local url="$1" tries="${2:-15}"
  for _ in $(seq 1 "$tries"); do
    curl -sf -m 2 "$url" >/dev/null && return 0
    sleep 1
  done
  return 1
}

echo -n "embed:    "
if wait_health "${EMBED_URL}/health"; then
  curl -s -m 3 "${EMBED_URL}/health"; echo
else
  echo "(未起，看 /tmp/embed.log)"
fi

echo -n "rerank:   "
if wait_health "${RERANK_URL}/health"; then
  curl -s -m 3 "${RERANK_URL}/health"; echo
else
  echo "(未起，看 /tmp/rerank.log)"
fi

echo -n "admin:    "; curl -s -m 3 -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:${ADMIN_PORT_NUM}/health"
echo -n "weaviate: "; curl -s -m 3 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/v1/.well-known/ready
echo -n "neo4j:    "; curl -s -m 3 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7474
