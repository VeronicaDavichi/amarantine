"""把 pytorch_model.bin 转成 model.safetensors。

原因：TEI（Rust/candle 后端）只加载 safetensors，而 BAAI/bge-m3 仓库
只提供 pytorch_model.bin，所以必须先转换。

用法（back_end 目录下）：
    uv run python ..\\harness\\convert_to_safetensors.py

产物：models/bge-m3/model.safetensors（约 2.2 GB）
"""

from __future__ import annotations

import sys
from pathlib import Path

import torch
from safetensors.torch import save_file

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "bge-m3"
SRC = MODEL_DIR / "pytorch_model.bin"
DST = MODEL_DIR / "model.safetensors"


def main() -> int:
    if not SRC.exists():
        print(f"缺少源文件: {SRC}")
        print("先跑 harness/fetch-model-modelscope.sh")
        return 1

    if DST.exists():
        print(f"已存在，跳过: {DST} ({DST.stat().st_size / 1024 / 1024:.0f} MB)")
        return 0

    print(f"加载 {SRC.name} ({SRC.stat().st_size / 1024 / 1024:.0f} MB) ...")
    # mmap=True：按需映射而不是整体读入内存。
    # 本机内存紧张时（可用 < 3 GB）普通加载会被 OOM 杀掉。
    state = torch.load(SRC, map_location="cpu", mmap=True, weights_only=True)

    if not isinstance(state, dict):
        print(f"意外的结构类型: {type(state)}")
        return 1

    # 常见前缀兼容；contiguous 的张量直接复用，避免复制导致内存翻倍
    cleaned = {}
    for key, value in state.items():
        if not hasattr(value, "is_contiguous"):
            print(f"跳过非张量项: {key} ({type(value)})")
            continue
        cleaned[key.replace("module.", "", 1)] = (
            value if value.is_contiguous() else value.contiguous()
        )

    print(f"张量数: {len(cleaned)}，开始写入 safetensors ...")
    save_file(cleaned, DST, metadata={"format": "pt"})

    print(f"完成: {DST} ({DST.stat().st_size / 1024 / 1024:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
