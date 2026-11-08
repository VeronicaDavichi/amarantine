#!/usr/bin/env python3
"""本地 rerank 服务（bge-reranker-v2-m3，GPU），替代 TEI 容器。

契约见 `back_end/src/memory/services/rerank.py`（TEI 的 `/rerank`）：

    POST /rerank  {"query": "...", "texts": ["..."], "model": "..."}
    → [{"index": 0, "score": 0.87}, ...]      # 顺序不保证，客户端自己排

为什么自己起：这台机器拉 TEI 镜像的 1GB+ 层反复超时；而 cross-encoder 在
4060 Ti 上打分 50 条只要几十毫秒，比容器还省事。

**注意**：打分必须用同一个模型（bge-reranker-v2-m3），拿 embedding 模型
（bge-m3）来打分是没意义的 —— 这也是为什么单独下了一份权重。

用法：
    /home/veronica/.venv-amarantine/bin/python harness/rerank_server.py

配置全部走环境变量（见 `back_end/.env`）：

    RERANK_BASE_URL       绑这个 URL 的 host:port（默认 http://127.0.0.1:8083）
                          与客户端同一个变量；对外服务填 http://0.0.0.0:8083
    RERANK_API_KEY        配了就校验 Bearer；为空不校验（与客户端同名）
    RERANK_PRELOAD=1      启动即加载
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from transformers import AutoModelForSequenceClassification, AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "bge-reranker-v2-m3"
ENV_FILE = ROOT / "back_end" / ".env"
MAX_LEN = 512


def _load_dotenv() -> None:
    """把 back_end/.env 读进环境变量（真实环境变量优先）。"""
    if not ENV_FILE.is_file():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _listen_addr(url: str, default_port: int) -> tuple[str, int]:
    """从 URL 拆出绑定的 host:port（与客户端共用 BASE_URL，不再另配两个变量）。"""
    parts = urlsplit(url if "://" in url else f"http://{url}")
    return parts.hostname or "127.0.0.1", parts.port or default_port

app = FastAPI()
_state: dict[str, Any] = {}


def _check_auth(request: Request) -> None:
    """配了 `--api-key` 才校验（远端部署时应该配）。"""
    expected = _state.get("api_key")
    if not expected:
        return
    if request.headers.get("authorization", "") != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid api key")


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    model: str | None = None
    #: TEI 兼容：有的客户端会带
    truncate: bool | None = None


def _load() -> None:
    # 同 embed_server：不能 `if _state`，否则 api_key 会挡住模型加载
    if "model" in _state:
        return
    t0 = time.time()
    use_cuda = torch.cuda.is_available()
    dtype = torch.float16 if use_cuda else torch.float32

    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModelForSequenceClassification.from_pretrained(
        str(MODEL_DIR), torch_dtype=dtype
    )
    model.eval()
    if use_cuda:
        model = model.to("cuda")

    _state.update(tok=tok, model=model, dtype=dtype, cuda=use_cuda)
    print(
        f"[rerank] 就绪 {time.time() - t0:.1f}s | "
        f"device={'cuda' if use_cuda else 'cpu'} | dtype={dtype}",
        flush=True,
    )


def _score(query: str, texts: list[str], batch_size: int = 16) -> list[float]:
    tok, model, use_cuda = _state["tok"], _state["model"], _state["cuda"]
    out: list[float] = []
    with torch.inference_mode():
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            pairs = [[query, t] for t in chunk]
            enc = tok(
                pairs,
                padding=True,
                truncation=True,
                max_length=MAX_LEN,
                return_tensors="pt",
            )
            if use_cuda:
                enc = {k: v.to("cuda") for k, v in enc.items()}
            logits = model(**enc).logits.view(-1).float()
            out.extend(logits.cpu().tolist())
    return out


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": "bge-reranker-v2-m3",
        "device": "cuda" if _state.get("cuda") else ("cpu" if _state else "unloaded"),
    }


@app.get("/info")
def info() -> dict[str, Any]:
    return {"model_id": "BAAI/bge-reranker-v2-m3", "max_input_length": MAX_LEN}


@app.post("/rerank")
def rerank(req: RerankRequest, request: Request) -> list[dict[str, Any]]:
    _check_auth(request)
    if not req.query or not req.texts:
        raise HTTPException(status_code=422, detail="query / texts 不能为空")
    _load()
    t0 = time.time()
    scores = _score(req.query, req.texts)
    elapsed = round((time.time() - t0) * 1000, 1)
    print(f"[rerank] {len(req.texts)} 条 {elapsed}ms", flush=True)
    return [{"index": i, "score": s} for i, s in enumerate(scores)]


def main() -> int:
    _load_dotenv()
    host, port = _listen_addr(
        os.environ.get("RERANK_BASE_URL", "http://127.0.0.1:8083"), 8083
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=host)
    ap.add_argument("--port", type=int, default=port)
    ap.add_argument("--api-key", default=os.environ.get("RERANK_API_KEY") or None)
    ap.add_argument(
        "--preload",
        action="store_true",
        default=_env_flag("RERANK_PRELOAD"),
        help="环境变量 RERANK_PRELOAD=1 等价",
    )
    args = ap.parse_args()
    if args.api_key:
        _state["api_key"] = args.api_key
    if args.preload:
        _load()
    print(f"[rerank] 监听 {args.host}:{args.port}（来自 RERANK_BASE_URL）", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
