"""spec 056：POST /admin/realtime 单条即时入库。

## 覆盖

- 纯文本消息入库
- 图片概括成功 → `[图片：描述]` 写进 text
- 概括失败 → 保留占位符，消息不丢
- 概括开关关闭 → 不调模型
- 幂等：同一条重发返回同一 quote_id、created=False
- 无 source 的图片 → `[图片]`
- text 与 images 都空 → 400
"""

from __future__ import annotations

import base64
import pathlib

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token"


@pytest.fixture()
def png(tmp_path) -> pathlib.Path:
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    p = tmp_path / "t.png"
    p.write_bytes(data)
    return p


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    from memory.config import Settings
    from memory.entrypoints.admin_http import create_app
    from memory.services import raw_quote as rq

    settings = Settings()
    settings.MEMORY_NAMESPACE = "rt"
    settings.ADMIN_TOKEN = TOKEN
    settings.IMAGE_CAPTION_ENABLED = True

    db = tmp_path / "rt.db"
    # 端点内部会调 db_path_from_settings，用 monkeypatch 指到临时库
    import memory.entrypoints.admin_http as mod

    monkeypatch.setattr(mod, "db_path_from_settings", lambda cfg: db)
    monkeypatch.setattr(rq, "db_path_from_settings", lambda cfg: db)
    app = create_app(settings)
    return TestClient(app)


def _post(c: TestClient, body: dict):
    return c.post("/admin/realtime", json=body,
                  headers={"Authorization": f"Bearer {TOKEN}"})


def test_text_only(client) -> None:
    r = _post(client, {"namespace": "rt", "text": "今天天气不错", "ts": "2026-11-28T10:00:00+08:00",
                       "session_id": "s1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["created"] is True
    assert d["text"] == "今天天气不错"
    assert d["images"] == []


def test_image_caption_success(client, png, monkeypatch) -> None:
    from memory.services import image_caption as cap

    monkeypatch.setattr(cap, "chat", lambda *a, **kw: "一张橘猫趴在键盘上")
    r = _post(client, {
        "namespace": "rt", "text": "你看这个", "ts": "2026-11-28T10:00:00+08:00",
        "session_id": "s2",
        "images": [{"path": str(png), "hash": "ABCD", "ext": "png"}],
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert "[图片：一张橘猫趴在键盘上]" in d["text"]
    assert d["images"][0]["status"] == "caption"


def test_image_caption_failure_keeps_placeholder(client, png, monkeypatch) -> None:
    """概括失败必须保留占位符 —— 绝不丢消息。"""
    from memory.services import image_caption as cap
    from memory.services.llm import LLMError

    def boom(*a, **kw):
        raise LLMError("超时")

    monkeypatch.setattr(cap, "chat", boom)
    r = _post(client, {
        "namespace": "rt", "text": "图来了", "ts": "2026-11-28T10:01:00+08:00",
        "session_id": "s3",
        "images": [{"path": str(png), "hash": "DEAD", "ext": "png"}],
    })
    assert r.status_code == 200
    d = r.json()
    assert "DEAD.png" in d["text"], "占位符要保留 hash 与扩展名"
    assert d["images"][0]["status"] == "placeholder"


def test_caption_disabled_does_not_call_model(client, png, monkeypatch) -> None:
    from memory.services import image_caption as cap

    def fail(*a, **kw):
        raise AssertionError("关掉时不该调模型")

    monkeypatch.setattr(cap, "chat", fail)
    r = _post(client, {
        "namespace": "rt", "text": "x", "ts": "2026-11-28T10:02:00+08:00",
        "session_id": "s4", "skip_caption": True,
        "images": [{"path": str(png), "hash": "OFF1", "ext": "png"}],
    })
    assert r.status_code == 200
    assert "OFF1.png" in r.json()["text"]


def test_idempotent_same_payload(client) -> None:
    body = {"namespace": "rt", "text": "同一条", "ts": "2026-11-28T10:03:00+08:00",
            "session_id": "s5"}
    a = _post(client, body).json()
    b = _post(client, body).json()
    assert a["quote_id"] == b["quote_id"], "同内容必须落同一个 id"
    assert a["created"] is True
    assert b["created"] is False


def test_idempotent_when_caption_same(client, png, monkeypatch) -> None:
    """概括结果相同 → id 稳定（这是「先概括再算 id」的目的）。"""
    from memory.services import image_caption as cap

    monkeypatch.setattr(cap, "chat", lambda *a, **kw: "固定描述")
    body = {"namespace": "rt", "text": "y", "ts": "2026-11-28T10:04:00+08:00",
            "session_id": "s6",
            "images": [{"path": str(png), "hash": "H2", "ext": "png"}]}
    a = _post(client, body).json()
    b = _post(client, body).json()
    assert a["quote_id"] == b["quote_id"]
    assert b["created"] is False


def test_image_without_source(client) -> None:
    r = _post(client, {
        "namespace": "rt", "text": "啥也没有", "ts": "2026-11-28T10:05:00+08:00",
        "session_id": "s7", "images": [{}],
    })
    assert r.status_code == 200
    d = r.json()
    assert d["images"][0]["status"] == "no_source"
    assert "[图片]" in d["text"]


def test_empty_text_and_images_rejected(client) -> None:
    r = _post(client, {"namespace": "rt", "session_id": "s8"})
    assert r.status_code == 400


def test_requires_token(client) -> None:
    r = client.post("/admin/realtime", json={"namespace": "rt", "text": "x"})
    assert r.status_code in (401, 403)
