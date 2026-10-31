#!/usr/bin/env bash
# 拉取 P0 所需镜像 —— 拉不下来就一直重试，直到成功或 Ctrl+C
#
# 在 WSL 里执行：
#   bash /mnt/e/PycharmProjects/amarantine/harness/pull-images.sh
#
# 行为：
#   - 本地已存在的镜像直接跳过
#   - 失败后指数退避重试（5s → 60s 封顶），不停
#   - 每连续失败 5 次提示一次备选源
#
# 预期总量约 3.4 GB（不含 embedding 首次启动时再下的 2.2 GB 模型权重）

set -uo pipefail

INITIAL_SLEEP=5
MAX_SLEEP=60

# 0 = 无限重试（默认）；设成 3 就只试 3 次
# 用法： PULL_ATTEMPTS_LIMIT=3 bash harness/pull-images.sh
ATTEMPT_LIMIT="${PULL_ATTEMPTS_LIMIT:-0}"

# 格式：镜像|大小|用途|备选源
IMAGES=(
  "cr.weaviate.io/semitechnologies/weaviate:1.28.4|~200MB|向量库（必需）|semitechnologies/weaviate:1.28.4"
  "neo4j:5.26-community|~700MB|图库（B9 才用，可后补）|"
  "ghcr.io/huggingface/text-embeddings-inference:cpu-1.7|~2.5GB|embedding 推理（必需）|ghcr.nju.edu.cn/huggingface/text-embeddings-inference:cpu-1.7"
)

pull_forever() {
  local img="$1" size="$2" note="$3" fallback="$4"
  local attempt=0 sleep_s="$INITIAL_SLEEP"

  echo ""
  echo "=============================================="
  echo " 镜像: $img"
  echo " 大小: $size"
  echo " 用途: $note"
  echo "=============================================="

  if docker image inspect "$img" >/dev/null 2>&1; then
    echo "✅ 本地已存在，跳过"
    return 0
  fi

  while true; do
    attempt=$((attempt + 1))
    echo ""
    echo "--- 第 $attempt 次尝试  $(date '+%H:%M:%S') ---"

    if docker pull "$img"; then
      echo "✅ 成功: $img"
      return 0
    fi

    echo "❌ 失败，${sleep_s}s 后重试（Ctrl+C 中断本脚本）"

    if [ "$ATTEMPT_LIMIT" -gt 0 ] && [ "$attempt" -ge "$ATTEMPT_LIMIT" ]; then
      echo "已达到尝试上限 $ATTEMPT_LIMIT 次，跳过 $img"
      return 1
    fi

    # 失败 5 次的整数倍时提示备选源
    if [ $((attempt % 5)) -eq 0 ] && [ -n "$fallback" ]; then
      echo ""
      echo "   已连续失败 $attempt 次。可另开一个终端试备选源："
      echo "     docker pull $fallback"
      echo "   （拉到后本脚本会自动跳过，因为镜像名相同）"
      echo ""
    fi

    sleep "$sleep_s"
    sleep_s=$((sleep_s * 2))
    [ "$sleep_s" -gt "$MAX_SLEEP" ] && sleep_s="$MAX_SLEEP"
  done
}

failed=()
for entry in "${IMAGES[@]}"; do
  IFS='|' read -r img size note fallback <<<"$entry"
  if ! pull_forever "$img" "$size" "$note" "$fallback"; then
    failed+=("$img")
  fi
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo ""
  echo "以下镜像未完成（去掉上限重跑即可继续）："
  printf '  ❌ %s\n' "${failed[@]}"
  exit 1
fi

echo ""
echo "================ 全部就位 ================"
docker images --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}" \
  | grep -Ei "weaviate|neo4j|text-embeddings" || true

echo ""
echo "下一步："
echo "  cd /mnt/e/PycharmProjects/amarantine && docker compose up -d"
