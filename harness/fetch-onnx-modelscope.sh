#!/usr/bin/env bash
# 下载 bge-m3 的 ONNX 版本，供 TEI 的 ORT 后端使用。
#
# 为什么需要：本机 CPU（i7-1360P / Raptor Lake）不支持 AVX512，
# 而 TEI 的 Candle+MKL 后端在该 CPU 上 warmup 阶段 SIGILL 崩溃。
# ORT（onnxruntime）会按 CPU 能力选择 kernel，兼容性更好。
#
# 用法：
#   bash harness/fetch-onnx-modelscope.sh [目标目录]
#
# 产物落到 <目标>/onnx/，TEI 启动时会优先尝试 ORT 后端。

set -uo pipefail

BASE="https://modelscope.cn/models/BAAI/bge-m3/resolve/master"
DEST="${1:-/mnt/e/PycharmProjects/amarantine/models/bge-m3}"

FILES=(
  onnx/model.onnx
  onnx/model.onnx_data
  onnx/config.json
  onnx/tokenizer.json
  onnx/tokenizer_config.json
  onnx/special_tokens_map.json
  onnx/sentencepiece.bpe.model
  onnx/Constant_7_attr__value
)

mkdir -p "$DEST/onnx"

for f in "${FILES[@]}"; do
  out="$DEST/$f"
  echo ">>> $f"
  curl -L -C - --retry 5 --retry-delay 3 --retry-all-errors \
    --connect-timeout 20 -s -S -o "$out" "$BASE/$f"
  rc=$?
  if [ $rc -ne 0 ] && [ $rc -ne 33 ]; then
    echo "!! 失败($rc): $f"
    exit 1
  fi
done

echo ""
echo "================ 完成 ================"
du -sh "$DEST/onnx"
ls -la "$DEST/onnx"
