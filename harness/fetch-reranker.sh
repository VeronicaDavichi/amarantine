#!/usr/bin/env bash
# 从 ModelScope 拉取 rerank 模型（默认 BAAI/bge-reranker-v2-m3）。
#
# 用法：
#   bash harness/fetch-reranker.sh                      # 默认 v2-m3 → models/bge-reranker-v2-m3
#   bash harness/fetch-reranker.sh bge-reranker-base    # 换小的（278M）
#   bash harness/fetch-reranker.sh "" /tmp/my-model     # 自定义目标目录
#
# 为什么单独一个脚本（不复用 fetch-model-modelscope.sh）：
#   bge-m3 的 ModelScope 仓库只放 pytorch_model.bin，TEI 需要 safetensors，
#   还得再跑 convert_to_safetensors.py。
#   而 bge-reranker-* 的仓库**直接有 model.safetensors**，下载即用，不用转换。
#
# 下完之后：
#   1) docker compose --profile rerank up -d reranker
#   2) .env 里 RERANK_ENABLED=true
#   3) 重启 admin 后端
#   没下模型也不要紧：RERANK_ENABLED=false 时召回完全正常（见 A27 降级路径）。

set -uo pipefail

MODEL_NAME="${1:-bge-reranker-v2-m3}"
DEST="${2:-/mnt/e/PycharmProjects/amarantine/models/${MODEL_NAME}}"

case "${MODEL_NAME}" in
  bge-reranker-v2-m3) REPO="BAAI/bge-reranker-v2-m3" ;;
  bge-reranker-base)  REPO="BAAI/bge-reranker-base" ;;
  *)                  REPO="${MODEL_NAME}" ;;
esac

BASE="https://modelscope.cn/models/${REPO}/resolve/master"

# XLM-RoBERTa 系需要 sentencepiece.bpe.model；bert 系没有这个文件，失败可忽略
FILES=(
  config.json
  configuration.json
  model.safetensors
  special_tokens_map.json
  tokenizer.json
  tokenizer_config.json
  sentencepiece.bpe.model
)

mkdir -p "${DEST}" || exit 1
echo "模型 : ${REPO}"
echo "目标 : ${DEST}"
echo

fail=0
for f in "${FILES[@]}"; do
  if [[ -s "${DEST}/${f}" ]]; then
    echo "  跳过 ${f}（已存在）"
    continue
  fi
  code=$(curl -L --max-time 600 -w "%{http_code}" -o "${DEST}/${f}.part" "${BASE}/${f}" 2>/dev/null)
  if [[ "${code}" == "200" ]]; then
    mv "${DEST}/${f}.part" "${DEST}/${f}"
    echo "  OK   ${f}"
  else
    rm -f "${DEST}/${f}.part"
    if [[ "${f}" == "sentencepiece.bpe.model" ]]; then
      echo "  略过 ${f}（bert 系没有，正常）"
    else
      echo "  FAIL ${f}（http ${code}）"
      fail=1
    fi
  fi
done

echo
if [[ -f "${DEST}/model.safetensors" && -f "${DEST}/config.json" ]]; then
  echo "完成。启动："
  echo "  docker compose --profile rerank up -d reranker"
  echo "然后 .env 设 RERANK_ENABLED=true 并重启后端。"
else
  echo "缺少 model.safetensors 或 config.json —— TEI 加载不了。" >&2
fi
exit "${fail}"
