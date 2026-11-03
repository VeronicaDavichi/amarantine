"""005 原文层：ingest 批量导入 + raw_quote 溯源。

`split_events` / `remember_event` 全是假的（不调 LLM），
但 `raw_quote` / `ingest_job` 两个表用的是**真的 SQLite** ——
这些断言要验的是「到底写没写进去」，不是「调没调」。

**测试里 202 会看到 status=done**：Starlette 的 TestClient 在
`client.post()` 返回前就把 BackgroundTasks 跑完了。生产环境里
202 那一刻是 `queued`。所以 A4 断言的是「202 + job_id 非空」，
状态用 `in (queued/running/done)`，并在 A9 单独验「跑完了是 done」。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from memory.config import Settings
from memory.entrypoints.admin_http import create_app
from memory.services.raw_quote import (
    JOB_DONE,
    JOB_QUEUED,
    RawQuoteStore,
    db_path_from_settings,
    quote_id_for,
)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NS = "work"


class _FakeRememberResult:
    def __init__(self, summary: str) -> None:
        self.event_id = "evt_" + summary[:8]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    settings = Settings()
    settings.MEMORY_NAMESPACE = NS
    settings.ADMIN_TOKEN = TOKEN
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'rq.sqlite3').as_posix()}"

    calls: dict[str, list[Any]] = {"split": [], "remember": []}

    import memory.services.llm_split as split_mod
    import memory.services.remember as remember_mod
    import memory.entrypoints.admin_http as mod

    def fake_split(text: str, settings: Any, **kw: Any) -> list[str]:
        calls["split"].append(text)
        return ["主人接受新 offer", "通勤一小时"]

    def fake_remember(**kw: Any) -> _FakeRememberResult:
        calls["remember"].append(
            {
                "summary": kw["summary"],
                "raw_quote_ids": list(kw.get("raw_quote_ids") or []),
            }
        )
        return _FakeRememberResult(kw["summary"])

    monkeypatch.setattr(split_mod, "split_events", fake_split)
    monkeypatch.setattr(remember_mod, "remember_event", fake_remember)

    # GET /raw 的反查要扫 Weaviate；这里给它一个可控的卡列表
    cards: list[dict[str, Any]] = []

    class FakeStore:
        def list_all(self, namespace: str, *, limit: int = 1000):
            return list(cards)

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "WeaviateStore", lambda cfg: FakeStore())

    app = create_app(settings)
    return TestClient(app), settings, calls, cards


def _turn(text: str, **over: Any) -> dict[str, Any]:
    base = {"text": text, "role": "user", "ts": "2026-10-30T10:21:14+08:00"}
    base.update(over)
    return base


# ---------------- 存储层：A1–A3 ----------------


def test_raw_quote_insert_is_idempotent(env) -> None:
    """A1：同 quote_id 重复写只留一行，原文不被改写。"""
    _, settings, _, _ = env
    store = RawQuoteStore(db_path_from_settings(settings))
    qid = "msg_test"

    assert store.put(
        namespace=NS, quote_id=qid, text="原文", role="user", source="chat"
    ) is True
    assert store.put(
        namespace=NS, quote_id=qid, text="原文", role="user", source="chat"
    ) is False

    assert store.count(NS) == 1
    got = store.get(NS, qid)
    assert got is not None and got.text == "原文"


def test_quote_id_is_stable() -> None:
    """A2：同 turn 算两次得到同一个 id —— 幂等的基础。"""
    kw = dict(
        namespace=NS,
        source="chat",
        session_id="s_1",
        ts="2026-10-30T10:21:14+08:00",
        text="主人接受新 offer",
    )
    assert quote_id_for(**kw) == quote_id_for(**kw)
    assert quote_id_for(**kw).startswith("msg_")


def test_quote_id_differs_per_namespace() -> None:
    """A3：同一段对话导入两个域要各自可查，不能互相覆盖。"""
    base = dict(
        source="chat",
        session_id="s_1",
        ts="2026-10-30T10:21:14+08:00",
        text="主人接受新 offer",
    )
    a = quote_id_for(namespace="work", **base)
    b = quote_id_for(namespace="personal", **base)
    assert a != b


def test_raw_quote_rejects_bad_role(env) -> None:
    _, settings, _, _ = env
    store = RawQuoteStore(db_path_from_settings(settings))
    with pytest.raises(ValueError, match="role"):
        store.put(namespace=NS, quote_id="msg_x", text="x", role="admin", source="chat")


# ---------------- ingest：A4–A10 ----------------


def test_ingest_returns_202_and_job_id(env) -> None:
    """A4：202 + job_id 非空。状态在 TestClient 里已经是 done（见模块 docstring）。"""
    client, _, _, _ = env
    r = client.post(
        "/admin/ingest",
        json={"dedupe_key": "k1", "turns": [_turn("我决定接受 offer 了")]},
        headers=AUTH,
    )

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["job_id"]
    assert body["status"] in (JOB_QUEUED, "running", JOB_DONE)


def test_ingest_persists_raw_quote_before_returning(env) -> None:
    """A5：202 返回时原文已经能查到 —— 不是等后台跑完才写。"""
    client, settings, _, _ = env
    r = client.post(
        "/admin/ingest",
        json={"dedupe_key": "k2", "turns": [_turn("我决定接受 offer 了")]},
        headers=AUTH,
    )
    assert r.status_code == 202
    qid = quote_id_for(
        namespace=NS,
        source="chat",
        session_id=None,
        ts="2026-10-30T10:21:14+08:00",
        text="我决定接受 offer 了",
    )
    got = RawQuoteStore(db_path_from_settings(settings)).get(NS, qid)
    assert got is not None and got.text == "我决定接受 offer 了"


def test_ingest_dedupe_returns_same_job(env) -> None:
    """A6：同 dedupe_key 再提交 → 同一个 job_id，且不重跑。"""
    client, _, calls, _ = env
    payload = {"dedupe_key": "k3", "turns": [_turn("同一批对话")]}

    first = client.post("/admin/ingest", json=payload, headers=AUTH)
    assert first.status_code == 202
    split_calls_after_first = len(calls["split"])

    second = client.post("/admin/ingest", json=payload, headers=AUTH)
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    # 没有第二次切分 = 真的没重跑
    assert len(calls["split"]) == split_calls_after_first


def test_ingest_all_do_not_store_is_200_rejected(env) -> None:
    """A7：整批被拦 → **200**（不是 202），job_id 为 null。"""
    client, _, calls, _ = env
    r = client.post(
        "/admin/ingest",
        json={
            "dedupe_key": "k4",
            "turns": [
                _turn("别记这个", do_not_store=True),
                _turn("也别记", do_not_store=True),
            ],
        },
        headers=AUTH,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "do_not_store"
    assert body["job_id"] is None
    assert body["events_created"] == 0
    # 压根没跑切分和写卡
    assert calls["split"] == []
    assert calls["remember"] == []


def test_ingest_partial_do_not_store_skips(env) -> None:
    """A8：部分命中 → 202，turns_skipped 等于被跳过条数。"""
    client, _, calls, _ = env
    r = client.post(
        "/admin/ingest",
        json={
            "dedupe_key": "k5",
            "turns": [
                _turn("这条要记"),
                _turn("这条别记", do_not_store=True),
            ],
        },
        headers=AUTH,
    )

    assert r.status_code == 202, r.text
    assert r.json()["turns_skipped"] == 1
    assert r.json()["turns_total"] == 2
    assert len(calls["remember"]) == 2  # 切分出了两条摘要


def test_ingest_job_finishes_done(env) -> None:
    """A9：后台跑完 → done 且 events_created >= 1。"""
    client, _, _, _ = env
    r = client.post(
        "/admin/ingest",
        json={"dedupe_key": "k6", "turns": [_turn("批量导入的一批")]},
        headers=AUTH,
    )
    job_id = r.json()["job_id"]

    got = client.get(f"/admin/ingest/{job_id}", headers=AUTH)
    assert got.status_code == 200, got.text
    assert got.json()["status"] == JOB_DONE
    assert got.json()["events_created"] >= 1


def test_cards_carry_exactly_batch_quote_ids(env) -> None:
    """A10：卡上的 raw_quote_ids 恰好是本批那些 quote。

    「恰好」= 集合相等，不是「包含」—— 多带一个 id 就说明串批次了。
    """
    client, settings, calls, _ = env
    turns = [_turn("第一句"), _turn("第二句", role="assistant")]
    r = client.post(
        "/admin/ingest", json={"dedupe_key": "k7", "turns": turns}, headers=AUTH
    )
    assert r.status_code == 202

    expected = {
        quote_id_for(
            namespace=NS,
            source="chat",
            session_id=None,
            ts=t["ts"],
            text=t["text"],
        )
        for t in turns
    }
    assert len(calls["remember"]) >= 1
    for rec in calls["remember"]:
        assert set(rec["raw_quote_ids"]) == expected


# ---------------- 溯源：A11–A14 ----------------


def test_get_raw_returns_text_and_event_ids(env) -> None:
    """A11：GET /raw → 200，text 与导入一致，event_ids 非空。"""
    client, _, _, cards = env
    turns = [_turn("我要被溯源")]
    r = client.post(
        "/admin/ingest", json={"dedupe_key": "k8", "turns": turns}, headers=AUTH
    )
    assert r.status_code == 202

    qid = quote_id_for(
        namespace=NS,
        source="chat",
        session_id=None,
        ts=turns[0]["ts"],
        text=turns[0]["text"],
    )
    # 真库场景里卡由 remember 写进 Weaviate；这里手放一张带该 quote 的卡
    cards.append({"event_id": "evt_abc", "raw_quote_ids": [qid]})

    got = client.get(f"/admin/raw/{qid}", headers=AUTH)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["text"] == "我要被溯源"
    assert body["event_ids"] == ["evt_abc"]


def test_get_raw_missing_is_404(env) -> None:
    """A12"""
    client, _, _, _ = env
    r = client.get("/admin/raw/msg_nope", headers=AUTH)
    assert r.status_code == 404


def test_get_raw_survives_soft_delete(env) -> None:
    """A13：软删事件卡后原文仍 200 且 event_ids 仍能反查到它（A32）。

    `raw_quote` 没有删除路径，所以这条**必须**过 ——
    它防的是「以后有人顺手给忘了的卡也删掉原文」。
    """
    client, _, _, cards = env
    turns = [_turn("忘掉也要能溯源")]
    client.post(
        "/admin/ingest", json={"dedupe_key": "k9", "turns": turns}, headers=AUTH
    )
    qid = quote_id_for(
        namespace=NS,
        source="chat",
        session_id=None,
        ts=turns[0]["ts"],
        text=turns[0]["text"],
    )
    cards.append(
        {"event_id": "evt_del", "raw_quote_ids": [qid], "is_deleted": True}
    )

    got = client.get(f"/admin/raw/{qid}", headers=AUTH)
    assert got.status_code == 200, got.text
    assert got.json()["text"] == "忘掉也要能溯源"
    assert got.json()["event_ids"] == ["evt_del"]


def test_get_ingest_job_and_404(env) -> None:
    """A14：查得到 job；不存在 → 404。"""
    client, _, _, _ = env
    r = client.post(
        "/admin/ingest",
        json={"dedupe_key": "k10", "turns": [_turn("查进度")]},
        headers=AUTH,
    )
    job_id = r.json()["job_id"]

    ok = client.get(f"/admin/ingest/{job_id}", headers=AUTH)
    assert ok.status_code == 200
    assert ok.json()["job_id"] == job_id
    assert ok.json()["turns_total"] == 1

    missing = client.get("/admin/ingest/job_nope", headers=AUTH)
    assert missing.status_code == 404


# ---------------- 边界 ----------------


def test_ingest_requires_dedupe_key(env) -> None:
    client, _, _, _ = env
    r = client.post(
        "/admin/ingest", json={"dedupe_key": "  ", "turns": [_turn("x")]}, headers=AUTH
    )
    assert r.status_code == 400


def test_ingest_rejects_empty_turns(env) -> None:
    client, _, _, _ = env
    r = client.post(
        "/admin/ingest", json={"dedupe_key": "k11", "turns": []}, headers=AUTH
    )
    assert r.status_code == 400


def test_ingest_rejects_all_invalid_turns(env) -> None:
    client, _, _, _ = env
    r = client.post(
        "/admin/ingest",
        json={"dedupe_key": "k12", "turns": [{"text": "  ", "role": "user"}]},
        headers=AUTH,
    )
    assert r.status_code == 400


def test_ingest_requires_token(env) -> None:
    client, _, _, _ = env
    r = client.post(
        "/admin/ingest", json={"dedupe_key": "k13", "turns": [_turn("x")]}
    )
    assert r.status_code == 401


def test_card_failure_does_not_inflate_turns_skipped(env, monkeypatch) -> None:
    """写卡失败 ≠ turn 被跳过。两个计数混一起，沉默损失就看不见了。

    `turns_skipped` 只记「被 do_not_store 拦下的 turn」；
    写卡失败进 `errors` + 让 `events_created` 不涨。所以
    `events_created + turns_skipped < turns_total` 时，差额就是丢掉的。
    """
    client, _, _, _ = env
    import memory.services.remember as remember_mod

    def boom(**kw):
        raise RuntimeError("LLM 超时")

    monkeypatch.setattr(remember_mod, "remember_event", boom)

    r = client.post(
        "/admin/ingest",
        json={"dedupe_key": "k_fail", "turns": [_turn("这条会失败")]},
        headers=AUTH,
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    got = client.get(f"/admin/ingest/{job_id}", headers=AUTH).json()
    assert got["events_created"] == 0
    # 关键：不是 1 —— 没有任何 turn 被 do_not_store 拦下
    assert got["turns_skipped"] == 0
    assert got["status"] == "failed"
    assert "LLM 超时" in (got["error"] or "")
