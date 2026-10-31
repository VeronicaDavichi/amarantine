"""下载 bge-m3 模型到本地目录，供 TEI 以挂载路径加载。

为什么需要它：TEI 的下载器要求 HTTP Range（Content-Range 头），而 hf-mirror
经过 307 重定向后返回 200 全量内容，导致 `Header content-range is missing`。
预先把模型落到磁盘、用 `--model-id /models/bge-m3` 启动即可绕开。

用法（项目根或 back_end 均可）：
    uv run --with huggingface_hub python harness/download_model.py

产物：models/bge-m3/（约 2.2 GB，已在 .gitignore 中排除）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# hf-mirror 不支持 xet 存储协议，禁用以免卡住
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from huggingface_hub import snapshot_download  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "models" / "bge-m3"


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    print(f"HF_ENDPOINT = {os.environ['HF_ENDPOINT']}")
    print(f"目标目录   = {TARGET}")
    print("开始下载（约 2.2 GB，支持断点续传，可 Ctrl+C 后重跑）...")

    path = snapshot_download(
        repo_id="BAAI/bge-m3",
        local_dir=str(TARGET),
        max_workers=2,
        # 只取推理必需的权重与配置；imgs/ 与 .DS_Store 等在 hf-mirror 上会 403
        ignore_patterns=[
            "imgs/*",
            "*.jpg",
            "*.png",
            ".DS_Store",
            "*/.DS_Store",
            "onnx/*",
            "*.onnx",
            "*.h5",
            "*.msgpack",
            "*.ot",
            "*.md",
        ],
    )

    total = sum(f.stat().st_size for f in TARGET.rglob("*") if f.is_file())
    print(f"完成: {path}")
    print(f"总大小: {total / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
