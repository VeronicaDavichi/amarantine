#!/usr/bin/env bash
# 唤醒本地依赖（WSL 空闲会自动关闭，容器跟着停）。
#
# 现象：一段时间不用 WSL，再跑脚本会报
#   ConnectionRefusedError: [Errno 111] Connection refused（连 7474 / 8081 失败）
# 这不是代码问题 —— WSL 实例被回收，Docker 容器随之 stop。
#
# 用法：
#   bash harness/wake.sh          # 起容器 + 探活
#   bash harness/wake.sh --relay  # 顺便重启 Windows 侧 LLM 中继（3001）
set -uo pipefail

CONTAINERS=(amarantine-weaviate amarantine-neo4j amarantine-embedding amarantine-reranker)
FAIL=0

echo "=== 起容器 ==="
for c in "${CONTAINERS[@]}"; do
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
    echo "  ✓ $c 已在运行"
  else
    out="$(docker start "$c" 2>&1)"
    [ -n "$out" ] && echo "  ↑ $c"
  fi
done

echo "=== 探活（最多 90s）==="
ready() {
  local name="$1" url="$2" i
  for i in $(seq 1 45); do
    if curl -s -m 2 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null | grep -q 200; then
      echo "  ✓ $name"
      return 0
    fi
    sleep 2
  done
  echo "  ✗ $name 未就绪"
  return 1
}
ready "weaviate (8081)" "http://127.0.0.1:8081/v1/.well-known/ready" || FAIL=1
ready "neo4j   (7474)" "http://127.0.0.1:7474/" || FAIL=1
ready "embedding (8082)" "http://127.0.0.1:8082/health" || echo "  · embedding 可能由 up_local.sh 起的本机进程提供"
ready "rerank   (8083)" "http://127.0.0.1:8083/health" || echo "  · rerank 同上"

if [ "${1:-}" = "--relay" ]; then
  echo "=== LLM 中继（Windows 侧 3001）==="
  if timeout 3 bash -c ':</dev/tcp/198.51.100.1/3001' 2>/dev/null; then
    echo "  ✓ 3001 可连"
  else
    echo "  ✗ 3001 不通；在 Windows 侧执行："
    echo '     cd E:\PycharmProjects\amarantine'
    echo '     .\back_end\.venv\Scripts\python.exe harness\vpn_relay.py --listen 3001 --target 198.51.100.58:3000'
    FAIL=1
  fi
fi

[ "$FAIL" -eq 0 ] && echo "全部就绪" || echo "有服务未就绪，见上"
exit "$FAIL"
