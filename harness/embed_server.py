#!/usr/bin/env python3
"""本地 OpenAI 兼容 embedding 服务（bge-m3，GPU/CPU 自适应），替代 TEI 容器。

为什么自己起服务：这台机器拉 `text-embeddings-inference` 的 1GB+ 层反复超时
（daocloud / 1ms.run 都卡在同一层）。契约本身很简单，见
`back_end/src/memory/services/embedding.py`：

    POST /v1/embeddings  {"input": ["..."], "model": "BAAI/bge-m3"}
    → {"data": [{"embedding": [...1024 floats...]}], "model": ...}

GPU：有 CUDA 就用 fp16（RTX 4060 Ti 16G 实测单批 8 条约 100ms 量级），
没有就退回 CPU fp32。**池化方式必须是 CLS** —— bge-m3 的 dense 向量定义如此，
换成 mean pooling 会让余弦相似度整体偏移，检索质量静默下降。

用法：
    /home/veronica/.venv-amarantine/bin/python harness/embed_server.py

配置全部走环境变量（见 `back_end/.env`，不用命令行参数）：

    EMBEDDING_BASE_URL    绑这个 URL 的 host:port（默认 http://127.0.0.1:8082）
                          与客户端**同一个变量** —— 单机时两边都填 127.0.0.1:8082；
                          对外提供服务时填 http://0.0.0.0:8082（本机客户端连 0.0.0.0 也通）
    EMBEDDING_API_KEY     配了就要求 Authorization: Bearer <key>；为空不校验
    EMBEDDING_PRELOAD=1   启动时就把模型读进显存（不等第一个请求）

`EMBEDDING_API_KEY` 与客户端用的也是**同一个变量名** —— 高配机在 .env 里配一行，
服务端校验和客户端发送同时生效，不会出现「一边配了另一边忘了」。
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
from transformers import AutoModel, AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "bge-m3"
ENV_FILE = ROOT / "back_end" / ".env"
MAX_LEN = 512
DIM = 1024


def _load_dotenv() -> None:
    """把 back_end/.env 读进环境变量（真实环境变量优先）。

    不引第三方依赖：格式就是 KEY=VALUE，注释用 #。
    目的是让「配置都在 .env」这件事对服务端也成立 —— 否则服务端还得记参数。
    """
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
    """从一个 URL 里拆出「绑哪个 host:port」。

    为什么要拆 URL 而不是配 HOST/PORT 两个变量：客户端已经有 BASE_URL 了，
    服务端再要一份 host+port 就是同一件事写三遍，很容易改一半。
    """
    parts = urlsplit(url if "://" in url else f"http://{url}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or default_port
    return host, port

app = FastAPI()
_state: dict[str, Any] = {}


def _check_auth(request: Request) -> None:
    """配了 `--api-key` 才校验；不配时保持旧行为（本地自用）。

    远端部署（低配机调用高配机的 8082）应当配上，否则同网段任何进程都能白嫖 GPU。
    """
    expected = _state.get("api_key")
    if not expected:
        return
    got = request.headers.get("authorization", "")
    if got != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid api key")


class EmbedRequest(BaseModel):
    input: list[str]
    model: str | None = None


def _load() -> None:
    # 必须判「model 在不在」而不是 `if _state` —— `_state` 里还可能先放了 api_key，
    # 那样会让模型永远不加载，第一次请求 500（这个坑被 auth 测试当场抓到）。
    if "model" in _state:
        return
    t0 = time.time()
    use_cuda = torch.cuda.is_available()
    dtype = torch.float16 if use_cuda else torch.float32

    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    # torch_dtype 在 CPU 上必须是 fp32（fp16 的 CPU 算子很多不支持）
    model = AutoModel.from_pretrained(str(MODEL_DIR), torch_dtype=dtype)
    model.eval()
    if use_cuda:
        model = model.to("cuda")

    _state.update(tok=tok, model=model, dtype=dtype, cuda=use_cuda)
    print(
        f"[embed] 就绪 {time.time() - t0:.1f}s | device={'cuda' if use_cuda else 'cpu'} "
        f"| dtype={dtype} | dim={DIM}",
        flush=True,
    )


def _encode(texts: list[str], batch_size: int = 16) -> list[list[float]]:
    tok, model, dtype, use_cuda = (
        _state["tok"],
        _state["model"],
        _state["dtype"],
        _state["cuda"],
    )
    out: list[list[float]] = []
    with torch.inference_mode():
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            enc = tok(
                chunk,
                padding=True,
                truncation=True,
                max_length=MAX_LEN,
                return_tensors="pt",
            )
            if use_cuda:
                enc = {k: v.to("cuda") for k, v in enc.items()}
            hidden = model(**enc).last_hidden_state[:, 0]  # CLS
            vecs = torch.nn.functional.normalize(hidden.float(), p=2, dim=1)
            out.extend(vecs.cpu().tolist())
    return out


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": "bge-m3",
        "device": "cuda" if _state.get("cuda") else ("cpu" if _state else "unloaded"),
    }


@app.get("/info")
def info() -> dict[str, Any]:
    return {"model_id": "BAAI/bge-m3", "max_input_length": MAX_LEN, "dim": DIM}


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest, request: Request) -> dict[str, Any]:
    _check_auth(request)
    if not req.input:
        raise HTTPException(status_code=422, detail="input 不能为空")
    _load()
    t0 = time.time()
    vectors = _encode(req.input)
    if vectors and len(vectors[0]) != DIM:
        raise HTTPException(status_code=500, detail=f"维度 {len(vectors[0])} != {DIM}")
    return {
        "object": "list",
        "model": req.model or "BAAI/bge-m3",
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
    }


def main() -> int:
    _load_dotenv()
    default_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:8082")
    host, port = _listen_addr(default_url, 8082)

    # 参数只作临时覆盖；正常启动不带任何参数，全从 .env 来
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=host)
    ap.add_argument("--port", type=int, default=port)
    ap.add_argument("--api-key", default=os.environ.get("EMBEDDING_API_KEY") or None)
    ap.add_argument(
        "--preload",
        action="store_true",
        default=_env_flag("EMBEDDING_PRELOAD"),
        help="环境变量 EMBEDDING_PRELOAD=1 等价",
    )
    args = ap.parse_args()
    if args.api_key:
        _state["api_key"] = args.api_key
    if args.preload:
        _load()
    print(f"[embed] 监听 {args.host}:{args.port}（来自 EMBEDDING_BASE_URL）", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
