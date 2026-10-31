#!/usr/bin/env bash
# 从 ModelScope 拉取 bge-m3（国内速度约 4.7 MB/s，比 hf-mirror 快约 100 倍）
#
# 用法：
#   bash harness/fetch-model-modelscope.sh [目标目录]
#
# 产出模型目录后，docker-compose 以 --model-id /models/bge-m3 挂载加载。
# 注意：仓库只提供 pytorch_model.bin，TEI 需要 safetensors，
#       下载完成后需再跑 harness/convert_to_safetensors.py。

set -uo pipefail

BASE="https://modelscope.cn/models/BAAI/bge-m3/resolve/master"
DEST="${1:-/mnt/e/PycharmProjects/amarantine/models/bge-m3}"

FILES=(
  config.json
  config_sentence_transformers.json
  configuration.json
  modules.json
  sentence_bert_config.json
  special_tokens_map.json
  tokenizer.json
  tokenizer_config.json
  sentencepiece.bpe.model
  colbert_linear.pt
  sparse_linear.pt
  1_Pooling/config.json
  pytorch_model.bin
)

mkdir -p "$DEST"

for f in "${FILES[@]}"; do
  out="$DEST/$f"
  mkdir -p "$(dirname "$out")"

  if [ -f "$out" ]; then
    echo ">>> 已存在，续传: $f"
  else
    echo ">>> 下载: $f"
  fi

  curl -L -C - --retry 5 --retry-delay 3 --retry-all-errors \
    --connect-timeout 20 \
    -o "$out" "$BASE/$f"

  rc=$?
  if [ $rc -ne 0 ] && [ $rc -ne 33 ]; then   # 33 = 服务端不支持续传，可忽略
    echo "!! 失败($rc): $f"
    exit 1
  fi
done

echo ""
echo "================ 完成 ================"
du -sh "$DEST"
ls -la "$DEST"
