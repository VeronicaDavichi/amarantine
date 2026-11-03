"""006 管理台：`GET /admin/events` + `/ui` 静态页。

前端没有 JS 测试框架，所以 D5/D6 用**源码结构断言**（读 index.html 找那两处
关键实现），不是行为断言。这比不测好，但必须诚实标注：它防的是「有人把
二次确认删了」，不防「二次确认写错了」。

真正的行为验证见 `_verify_ui.py` 的真库 + 真浏览器流程。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from memory.config import Settings
from memory.entrypoints.admin_http import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NS = "work"

_WEB = Path(__file__).resolve().parents[1] / "src" / "memory" / "entrypoints" / "web" / "index.html"


def _card(event_id: str, **over: Any) -> dict[str, Any]:
    base = {
        "event_id": event_id,
        "summary": f"摘要-{event_id}",
        "date_start": "2026-10-30T00:00:00Z",
        "types": ["decision"],
        "importance": "high",
        "pinned": False,
        "is_deleted": False,
        "raw_quote_ids": [],
        "recall_count": 0,
    }
    base.update(over)
    return base


@pytest.fixture()
def env(monkeypatch):
    calls: list[str] = []
    cards: dict[str, list[dict[str, Any]]] = {
        "work": [
            _card("evt_1", date_start="2026-10-30T00:00:00Z"),
            _card("evt_2", date_start="2026-11-02T00:00:00Z"),
            _card("evt_3", date_start="2026-11-01T00:00:00Z"),
        ],
        "personal": [_card("evt_p1")],
    }

    import memory.entrypoints.admin_http as mod

    class FakeStore:
        def __init__(self, cfg: Any) -> None:
            self.cfg = cfg

        def list_all(self, namespace: str, *, limit: int = 1000):
            calls.append(namespace)
            return list(cards.get(namespace, []))

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "WeaviateStore", lambda cfg: FakeStore(cfg))

    settings = Settings()
    settings.MEMORY_NAMESPACE = NS
    settings.ADMIN_TOKEN = TOKEN
    return TestClient(create_app(settings)), calls


# ---------------- D1 分页 ----------------


def test_events_paginates(env) -> None:
    client, _ = env
    r = client.get("/admin/events?namespace=work&limit=2&offset=0", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2

    page2 = client.get("/admin/events?namespace=work&limit=2&offset=2", headers=AUTH).json()
    assert page2["total"] == 3
    assert len(page2["items"]) == 1
    # 两页不重叠
    first_ids = {i["event_id"] for i in body["items"]}
    second_ids = {i["event_id"] for i in page2["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_events_sorted_by_date_desc(env) -> None:
    client, _ = env
    items = client.get("/admin/events?namespace=work", headers=AUTH).json()["items"]
    dates = [i["date_start"] for i in items]
    assert dates == sorted(dates, reverse=True), "列表最上面应该是最近的事"


# ---------------- D2 域隔离 ----------------


def test_events_respect_namespace(env) -> None:
    client, calls = env
    work = client.get("/admin/events?namespace=work", headers=AUTH).json()
    personal = client.get("/admin/events?namespace=personal", headers=AUTH).json()

    assert {i["event_id"] for i in work["items"]} == {"evt_1", "evt_2", "evt_3"}
    assert {i["event_id"] for i in personal["items"]} == {"evt_p1"}
    # 端点确实把调用方的域传下去了，而不是用了 MEMORY_NAMESPACE 兜底
    assert "personal" in calls


def test_events_falls_back_to_settings_namespace(env) -> None:
    client, calls = env
    r = client.get("/admin/events", headers=AUTH)
    assert r.status_code == 200
    assert calls[-1] == NS


# ---------------- D4 鉴权边界 ----------------


def test_ui_html_is_public_but_data_needs_token(env) -> None:
    client, _ = env

    # 空壳 HTML 不含任何秘密，挡它没有意义
    ui = client.get("/ui/")
    assert ui.status_code == 200
    assert "<html" in ui.text.lower()

    # 真正的边界在数据接口上
    assert client.get("/admin/events?namespace=work").status_code == 401
    # 带了 token 但不对 → 403（`HTTPBearer` 的既有行为，不是 401）。
    # 前端两个都要接：只判 401 的话，用户输错 token 会看到「通用错误」，
    # 而不是「去重填 token」。
    assert client.get(
        "/admin/events?namespace=work", headers={"Authorization": "Bearer wrong"}
    ).status_code == 403


def test_frontend_handles_both_401_and_403() -> None:
    """错 token 是 403、没 token 是 401 —— 前端必须两个都认。

    这条测试的由来：写前端时才发现既有实现里错 token 走 403，
    只判 401 会让「token 填错了」显示成「加载失败」。
    """
    html = _WEB.read_text(encoding="utf-8")
    assert "res.status === 401 || res.status === 403" in html


# ---------------- 参数边界 ----------------


def test_events_rejects_bad_paging(env) -> None:
    client, _ = env
    assert client.get("/admin/events?limit=0", headers=AUTH).status_code == 422
    assert client.get("/admin/events?limit=501", headers=AUTH).status_code == 422
    assert client.get("/admin/events?offset=-1", headers=AUTH).status_code == 422


def test_events_returns_shape(env) -> None:
    client, _ = env
    item = client.get("/admin/events?namespace=work", headers=AUTH).json()["items"][0]
    for key in (
        "event_id",
        "summary",
        "date_start",
        "types",
        "importance",
        "pinned",
        "is_deleted",
        "raw_quote_ids",
        "recall_count",
    ):
        assert key in item, f"列表项缺字段: {key}"


# ---------------- D5/D6 前端结构断言 ----------------


def test_switching_namespace_clears_view() -> None:
    """D5：切域必须清空视图，防止上一域的残行留在屏幕上。"""
    html = _WEB.read_text(encoding="utf-8")
    apply_block = html.split("$('#ns-apply').onclick", 1)
    assert len(apply_block) == 2, "找不到切域的处理函数"
    body = apply_block[1].split("};", 1)[0]
    assert "localStorage.setItem('mem.ns'" in body
    assert "$('#view').innerHTML = ''" in body, "切域没有清空视图"


def test_soft_delete_requires_typing_namespace() -> None:
    """D6：软删必须手输域名确认，且输入不符时**不删除**。

    这是没有登录的系统里唯一的误删闸门 —— 被删掉或被绕过都是真事故。
    """
    html = _WEB.read_text(encoding="utf-8")
    fn = html.split("async function softDelete", 1)
    assert len(fn) == 2, "找不到 softDelete"
    body = fn[1].split("\n}", 1)[0]
    assert "window.prompt" in body, "软删没有二次确认"
    assert "typed !== S.ns" in body, "确认逻辑没有比对域名"
    # 比对失败必须提前 return，不能继续往下走删除请求
    before_delete = body.split("typed !== S.ns", 1)[1].split("/admin/event/", 1)[0]
    assert "return" in before_delete, "域名不符时没有 return"


def test_sweep_is_hardcoded_dry_run() -> None:
    """A12：前端不允许触发自动遗忘 —— dry_run 是写死的，不是默认值。"""
    html = _WEB.read_text(encoding="utf-8")
    assert "dry_run: true" in html
    assert "dry_run: false" not in html
