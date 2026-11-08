"""embedding 远端的鉴权（混合部署：模型在高配机，低配机带 key 调用）。

背景：`EMBEDDING_API_KEY` 原本是**死配置** —— 定义在 config 里，但
`embedding.py` 从来不发 header。这一组测试钉住「配了就发、不配不发」。
"""

from __future__ import annotations

from memory.config import load_settings
from memory.services.embedding import embed_texts


class _Resp:
    def __init__(self, status: int, data) -> None:
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


class _Client:
    """httpx.Client 替身：记录 url / payload / headers。"""

    def __init__(self, *, status: int = 200, data=None) -> None:
        self.status = status
        self.data = data
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        return _Resp(self.status, self.data)

    def close(self) -> None:
        pass


def _settings(**overrides):
    s = load_settings()
    s.EMBEDDING_BASE_URL = "http://10.0.0.5:8082"
    s.EMBEDDING_MODEL = "BAAI/bge-m3"
    s.EMBEDDING_DIM = 1024
    s.EMBEDDING_API_KEY = None
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _payload_ok() -> dict:
    return {"data": [{"embedding": [0.01] * 1024}]}


def test_sends_bearer_when_key_configured() -> None:
    client = _Client(data=_payload_ok())
    embed_texts(["反洗钱前端"], _settings(EMBEDDING_API_KEY="sk-remote"), client=client)

    call = client.calls[0]
    assert call["headers"]["Authorization"] == "Bearer sk-remote"
    assert call["url"] == "http://10.0.0.5:8082/v1/embeddings"
    assert call["json"]["input"] == ["反洗钱前端"]


def test_no_header_when_key_empty() -> None:
    client = _Client(data=_payload_ok())
    embed_texts(["x"], _settings(), client=client)
    assert "Authorization" not in client.calls[0]["headers"]
