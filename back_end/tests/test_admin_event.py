"""004 事件管理面：PATCH / DELETE / archive / recall / feedback。

全部用假 store + monkeypatch，不碰真 Weaviate / Neo4j ——
真库证据在交付时单独跑（A1/A5 那两条必须真库才算数）。

**为什么 fake store 也值得一提**：这个文件里的断言都在验「状态码 + 到底有没有写」，
而不是「调了没调」。所以 fake 会记录 `updated` / `deleted`，
让「400 之后零写入」这种要求能被真正判定，而不是只看 code。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from memory.config import Settings
from memory.entrypoints.admin_http import create_app
from memory.services.forget import ForgetResult

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NS = "work"


class FakeEventStore:
    """记账用的假 store：既实现被调用的方法，也记录被写过的内容。"""

    def __init__(self, cards: dict[str, dict[str, Any]] | None = None) -> None:
        self.cards = cards if cards is not None else {}
        #: 每次 `update_fields` 收到的 properties（用来断言「零写入」）
        self.updated: list[dict[str, Any]] = []
        self.closed = 0
        self.older = 0
        self.scanned = 0

    def get_by_event_id(self, namespace: str, event_id: str) -> dict | None:
        card = self.cards.get(event_id)
        if card is None or card.get("namespace") != namespace:
            return None
        return dict(card)

    def update_fields(
        self, event_id: str, namespace: str, *, properties: dict[str, Any]
    ) -> bool:
        if event_id not in self.cards:
            return False
        self.updated.append(dict(properties))
        self.cards[event_id].update(properties)
        return True

    def count_older_than(
        self, namespace: str, *, cutoff: str, limit: int = 5000
    ) -> tuple[int, int]:
        return self.older, self.scanned

    def close(self) -> None:
        self.closed += 1


def _card(event_id: str = "evt_a", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_id": event_id,
        "namespace": NS,
        "summary": "主人决定接受新 offer",
        "pinned": False,
        "pinned_source": None,
        "retention": "normal",
        "is_deleted": False,
        "deleted_at": None,
        "updated_at": None,
    }
    base.update(over)
    return base


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """返回 (client, store)。store 就是端点内部真正用的那个。"""
    settings = Settings()
    settings.MEMORY_NAMESPACE = NS
    settings.ADMIN_TOKEN = TOKEN
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'ev.sqlite3').as_posix()}"

    store = FakeEventStore({"evt_a": _card()})

    import memory.entrypoints.admin_http as mod

    monkeypatch.setattr(mod, "WeaviateStore", lambda cfg: store)
    return TestClient(create_app(settings)), store, mod


# ---------------- PATCH：A1–A4 ----------------


def test_patch_pinned_sets_source_manual(env) -> None:
    """A1：pinned=true 必须同时留 pinned_source=manual，且真的写进去。"""
    client, store, _ = env
    r = client.patch("/admin/event/evt_a", json={"pinned": True}, headers=AUTH)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pinned"] is True
    assert body["pinned_source"] == "manual"
    # 不是「返回体说改了」——store 里真的改了
    assert store.cards["evt_a"]["pinned"] is True
    assert store.cards["evt_a"]["pinned_source"] == "manual"
    assert body["updated_at"] is not None


def test_patch_rejects_field_outside_whitelist(env) -> None:
    """A2：改 summary 必须 400，且**一个字节都没写**。

    这条如果只断言 400 是不够的：实现完全可能先写再报错。
    所以要查 `store.updated` 为空。
    """
    client, store, _ = env
    r = client.patch(
        "/admin/event/evt_a", json={"summary": "被偷偷改了"}, headers=AUTH
    )

    assert r.status_code == 400, r.text
    assert "summary" in r.json()["detail"]
    assert store.updated == []
    assert store.cards["evt_a"]["summary"] == "主人决定接受新 offer"


def test_patch_same_value_is_idempotent(env) -> None:
    """A3：重复置同一值 → 200 且 updated_at 不变（ADR 4.6 明写）。"""
    client, store, _ = env
    first = client.patch("/admin/event/evt_a", json={"pinned": True}, headers=AUTH)
    assert first.status_code == 200
    stamp = first.json()["updated_at"]
    assert stamp is not None

    store.updated.clear()
    second = client.patch("/admin/event/evt_a", json={"pinned": True}, headers=AUTH)

    assert second.status_code == 200
    assert second.json()["updated_at"] == stamp
    # 第二次不该再发写请求
    assert store.updated == []


def test_patch_missing_event_is_404(env) -> None:
    """A4"""
    client, _, _ = env
    r = client.patch("/admin/event/evt_gone", json={"pinned": True}, headers=AUTH)
    assert r.status_code == 404


def test_patch_requires_at_least_one_field(env) -> None:
    client, _, _ = env
    r = client.patch("/admin/event/evt_a", json={}, headers=AUTH)
    assert r.status_code == 400


def test_patch_requires_token(env) -> None:
    client, _, _ = env
    r = client.patch("/admin/event/evt_a", json={"pinned": True})
    assert r.status_code == 401


# ---------------- DELETE：A5–A8 ----------------


def _patch_forget(mod, store, *, raise_exc: Exception | None = None):
    """把 forget_event 换成「只改假 store」的版本。

    真 forget_event 会连 Neo4j 写图；单测里不需要那一段，
    但**软删这件事本身要真的发生**，否则 A5 就成了自证。
    """

    def fake_forget(**kwargs):
        if raise_exc is not None:
            raise raise_exc
        eid = kwargs["event_id"]
        store.cards[eid]["is_deleted"] = True
        store.cards[eid]["deleted_at"] = "2026-11-03T00:00:00+00:00"
        return ForgetResult(
            event_id=eid, forgotten=True, reason=kwargs["reason"], graph_ok=True
        )

    mod.forget_event = fake_forget


def test_delete_soft_deletes(env) -> None:
    """A5：软删 → is_deleted/deleted_at 真的落下，raw_quote_preserved 恒 true。"""
    client, store, mod = env
    _patch_forget(mod, store)

    r = client.request(
        "DELETE", "/admin/event/evt_a", json={"reason": "user_forget"}, headers=AUTH
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["soft_deleted"] is True
    assert body["raw_quote_preserved"] is True
    assert body["deleted_at"] == "2026-11-03T00:00:00+00:00"
    assert store.cards["evt_a"]["is_deleted"] is True


def test_delete_hard_is_rejected_without_side_effect(env) -> None:
    """A6：hard=true → 400，且卡**完全没动**。

    关键是「拒绝发生在入口」：不能先软删再报错，也不能静默降级成软删。
    """
    client, store, mod = env
    _patch_forget(mod, store)

    r = client.request(
        "DELETE",
        "/admin/event/evt_a",
        json={"reason": "user_forget", "hard": True},
        headers=AUTH,
    )

    assert r.status_code == 400, r.text
    assert "hard" in r.json()["detail"]
    assert store.cards["evt_a"]["is_deleted"] is False
    assert store.cards["evt_a"]["deleted_at"] is None


def test_delete_is_idempotent_keeps_original_timestamp(env) -> None:
    """A7：重复删 → 200 且 deleted_at **与原值相同**（不刷新）。"""
    client, store, mod = env
    store.cards["evt_a"]["is_deleted"] = True
    store.cards["evt_a"]["deleted_at"] = "2026-10-01T00:00:00+00:00"

    called = {"n": 0}

    def fake_forget(**kwargs):  # 不该被调
        called["n"] += 1
        raise AssertionError("已软删的卡不该再走 forget_event")

    mod.forget_event = fake_forget

    r = client.request(
        "DELETE", "/admin/event/evt_a", json={"reason": "user_forget"}, headers=AUTH
    )

    assert r.status_code == 200, r.text
    assert r.json()["deleted_at"] == "2026-10-01T00:00:00+00:00"
    assert called["n"] == 0


def test_delete_reason_enum_and_retention_expired(env) -> None:
    """A8：非法 reason → 400；`retention_expired` 必须可用（ADR 4.7）。"""
    client, store, mod = env
    _patch_forget(mod, store)

    bad = client.request(
        "DELETE", "/admin/event/evt_a", json={"reason": "nuke"}, headers=AUTH
    )
    assert bad.status_code == 400

    ok = client.request(
        "DELETE",
        "/admin/event/evt_a",
        json={"reason": "retention_expired"},
        headers=AUTH,
    )
    assert ok.status_code == 200, ok.text


def test_delete_missing_event_is_404(env) -> None:
    client, _, _ = env
    r = client.request("DELETE", "/admin/event/evt_gone", json={}, headers=AUTH)
    assert r.status_code == 404


# ---------------- archive：A9–A10 ----------------


def test_archive_reports_candidates_and_never_moves(env) -> None:
    """A9：默认 dry_run → moved=0 / mode=logical，candidates 来自 store。"""
    client, store, _ = env
    store.older, store.scanned = 7, 42

    r = client.post("/admin/archive", json={"window_days": 180}, headers=AUTH)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidates"] == 7
    assert body["scanned"] == 42
    assert body["moved"] == 0
    assert body["mode"] == "logical"
    assert body["window_days"] == 180


def test_archive_dry_run_false_still_moves_nothing(env) -> None:
    """A10：P0 里 `dry_run=false` 也一条都不搬（ADR 4.4）。

    这条防的是「顺手把搬迁实现了」—— 那会在误删率闸门没过时
    把卡挪出热区，检索直接变差。
    """
    client, store, _ = env
    store.older, store.scanned = 3, 10

    r = client.post(
        "/admin/archive", json={"window_days": 30, "dry_run": False}, headers=AUTH
    )

    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 0
    assert r.json()["mode"] == "logical"


def test_archive_rejects_bad_window(env) -> None:
    client, _, _ = env
    r = client.post("/admin/archive", json={"window_days": 0}, headers=AUTH)
    assert r.status_code == 400


# ---------------- recall / feedback：A11–A12 ----------------


class _FakeRecallResult:
    def __init__(self, trace_id: str | None) -> None:
        self._trace_id = trace_id

    def to_payload(self) -> dict[str, Any]:
        return {
            "materials": [{"event_id": "evt_a", "summary": "x"}],
            "degraded": False,
            "partial": False,
            "trace": {"k": 1},
            "trace_id": self._trace_id,
        }


def test_recall_returns_trace_id(env, monkeypatch) -> None:
    """A11：管理面召回必须带 trace_id —— 否则 feedback 无从关联。"""
    client, _, mod = env
    seen: dict[str, Any] = {}

    def fake_recall(query, **kwargs):
        seen["query"] = query
        seen["trace_store"] = kwargs.get("trace_store")
        return _FakeRecallResult("tr_abc")

    monkeypatch.setattr(mod, "recall", fake_recall)
    monkeypatch.setattr(mod, "trace_store_from_settings", lambda cfg: "FAKE_STORE")

    r = client.post("/admin/recall", json={"query": "offer"}, headers=AUTH)

    assert r.status_code == 200, r.text
    assert r.json()["trace_id"] == "tr_abc"
    assert r.json()["materials"]
    assert seen["query"] == "offer"
    assert seen["trace_store"] == "FAKE_STORE"


def test_recall_rejects_empty_query(env) -> None:
    client, _, _ = env
    r = client.post("/admin/recall", json={"query": "   "}, headers=AUTH)
    assert r.status_code == 400


def test_recall_unavailable_is_503(env, monkeypatch) -> None:
    client, _, mod = env

    def boom(*a, **k):
        raise mod.RecallUnavailableError("weaviate down")

    monkeypatch.setattr(mod, "recall", boom)
    r = client.post("/admin/recall", json={"query": "x"}, headers=AUTH)
    assert r.status_code == 503


def _outcome(**over: Any):
    from memory.services.recall_trace import FeedbackOutcome

    o = FeedbackOutcome()
    o.accepted = over.get("accepted", 1)
    o.deduped = over.get("deduped", 0)
    o.incremented = over.get("incremented", ["evt_a"])
    return o


def test_feedback_accepted(env, monkeypatch) -> None:
    """A12：正常上报 → 200 且 incremented 有内容。"""
    client, _, mod = env
    monkeypatch.setattr(mod, "submit_feedback", lambda **kw: _outcome())

    r = client.post(
        "/admin/recall/tr_abc/feedback",
        json={"used_material_ids": ["evt_a"]},
        headers=AUTH,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trace_id"] == "tr_abc"
    assert body["accepted"] == 1
    assert body["incremented"] == ["evt_a"]


def test_feedback_unknown_trace_is_404(env, monkeypatch) -> None:
    """A12 后半：trace 不存在 → 404，而「id 不在返回集」这类是 400。

    两者必须分得开，否则调用方不知道是该重试还是改参数。
    """
    client, _, mod = env

    def not_found(**kw):
        raise mod.TraceNotFoundError(f"trace_id 不存在或已过期: {kw['trace_id']}")

    monkeypatch.setattr(mod, "submit_feedback", not_found)
    r = client.post(
        "/admin/recall/tr_gone/feedback",
        json={"used_material_ids": ["evt_a"]},
        headers=AUTH,
    )
    assert r.status_code == 404


def test_feedback_bad_material_is_400(env, monkeypatch) -> None:
    client, _, mod = env

    def bad(**kw):
        raise mod.FeedbackError("以下 event_id 不在该 trace 的返回集里: evt_x")

    monkeypatch.setattr(mod, "submit_feedback", bad)
    r = client.post(
        "/admin/recall/tr_abc/feedback",
        json={"used_material_ids": ["evt_x"]},
        headers=AUTH,
    )
    assert r.status_code == 400


def test_feedback_requires_token(env) -> None:
    client, _, _ = env
    r = client.post(
        "/admin/recall/tr_abc/feedback", json={"used_material_ids": ["evt_a"]}
    )
    assert r.status_code == 401
