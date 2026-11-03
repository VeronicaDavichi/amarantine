"""Admin HTTP 单测（M5）。

覆盖三类风险：
1. **认证**：没 token / 错 token / 没配 token 三种情况必须分清
2. **A12**：`dry_run=false` 必须 400，不能悄悄真删
3. **桶枚举**：非法 bucket 拒绝（A24 也靠这个隔离队列）
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from memory.config import Settings
from memory.entrypoints.admin_http import create_app

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = TOKEN
    settings.FORGET_IDLE_DAYS = 180
    settings.FORGET_GRACE_DAYS = 90
    settings.FORGET_MIN_RECALLS = 1
    settings.PROTECT_IMPORTANCE_REVIEW = 0.7

    # 让管理接口拿到可控的数据集，不打真 Weaviate
    old = date.today() - timedelta(days=400)
    fake_rows = [
        # SAFE_TO_DELETE：老、没召回、importance 低
        {
            "event_id": "evt_old",
            "importance": 0.1,
            "recall_count": 0,
            "date_start": old.isoformat(),
            "last_recalled_at": None,
            "retention": "normal",
            "pinned": False,
            "unresolved": [],
            "summary": "很久没用的旧事",
        },
        # NEED_REVIEW：importance 高
        {
            "event_id": "evt_imp",
            "importance": 0.95,
            "recall_count": 0,
            "date_start": old.isoformat(),
            "last_recalled_at": None,
            "retention": "normal",
            "pinned": False,
            "unresolved": [],
            "summary": "很重要但没被用过",
        },
        # PROTECTED：pinned
        {
            "event_id": "evt_pin",
            "importance": 0.1,
            "recall_count": 0,
            "date_start": old.isoformat(),
            "last_recalled_at": None,
            "retention": "normal",
            "pinned": True,
            "unresolved": [],
            "summary": "钉住的事",
        },
    ]

    import memory.entrypoints.admin_http as mod

    monkeypatch.setattr(mod, "_load_all", lambda cfg, ns: _to_candidates(fake_rows))
    app = create_app(settings)
    return TestClient(app)


def _to_candidates(rows):
    from memory.core.forget_policy import ForgetCandidate

    return [
        ForgetCandidate(
            event_id=r["event_id"],
            importance=r["importance"],
            recall_count=r["recall_count"],
            created_at=date.fromisoformat(r["date_start"]),
            last_recalled_at=None,
            retention=r["retention"],
            pinned=r["pinned"],
            unresolved=r["unresolved"],
            review_verdict=r.get("review_verdict"),
            summary=r.get("summary"),
        )
        for r in rows
    ]


# ---------------- 认证 ----------------


def test_health_needs_no_auth(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_missing_token_is_401(client: TestClient) -> None:
    r = client.get("/admin/review")
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("www-authenticate", "")


def test_wrong_token_is_403(client: TestClient) -> None:
    r = client.get("/admin/review", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_wrong_scheme_rejected(client: TestClient) -> None:
    r = client.get("/admin/review", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


def test_no_token_configured_disables_endpoints() -> None:
    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = None
    c = TestClient(create_app(settings))
    r = c.get("/admin/review", headers=AUTH)
    assert r.status_code == 503


# ---------------- review ----------------


def test_review_need_review_bucket(client: TestClient) -> None:
    r = client.get("/admin/review?bucket=NEED_REVIEW", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    ids = [i["event_id"] for i in body["items"]]
    assert ids == ["evt_imp"]
    assert body["items"][0]["reason"] == "high_importance"
    assert body["total"] == 1


def test_review_protected_bucket(client: TestClient) -> None:
    r = client.get("/admin/review?bucket=PROTECTED", headers=AUTH)
    ids = [i["event_id"] for i in r.json()["items"]]
    assert "evt_pin" in ids


def test_review_invalid_bucket_rejected(client: TestClient) -> None:
    """A24：桶名必须受控，防止把 LABEL_REVIEW 混进来。"""
    r = client.get("/admin/review?bucket=LABEL_REVIEW", headers=AUTH)
    assert r.status_code == 400


def test_review_limit_validation(client: TestClient) -> None:
    assert client.get("/admin/review?limit=0", headers=AUTH).status_code == 422
    assert client.get("/admin/review?limit=9999", headers=AUTH).status_code == 422


def test_review_summary_attached(client: TestClient) -> None:
    r = client.get("/admin/review?bucket=NEED_REVIEW", headers=AUTH)
    assert r.json()["items"][0]["summary"] == "很重要但没被用过"


# ---------------- sweep ----------------


def test_sweep_dry_run_ok(client: TestClient) -> None:
    r = client.post("/admin/forget/sweep", json={"dry_run": True}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["buckets"]["SAFE_TO_DELETE"] == 1
    assert [s["event_id"] for s in body["sample"]] == ["evt_old"]


def test_sweep_non_dry_run_is_400(client: TestClient) -> None:
    """A12：误删率闸门未过，绝不允许真删。"""
    r = client.post("/admin/forget/sweep", json={"dry_run": False}, headers=AUTH)
    assert r.status_code == 400
    assert "dry_run" in r.json()["detail"]


def test_sweep_default_is_dry_run(client: TestClient) -> None:
    r = client.post("/admin/forget/sweep", json={}, headers=AUTH)
    assert r.status_code == 200


# ---------------- forget ----------------


def test_forget_rejects_bad_reason(client: TestClient) -> None:
    r = client.post(
        "/admin/forget",
        json={"event_id": "evt_old", "reason": "nuke"},
        headers=AUTH,
    )
    assert r.status_code == 400


def test_forget_requires_event_id(client: TestClient) -> None:
    r = client.post("/admin/forget", json={"event_id": ""}, headers=AUTH)
    assert r.status_code == 422


def test_forget_missing_event_is_404(client: TestClient, monkeypatch) -> None:
    import memory.entrypoints.admin_http as mod

    def boom(**kwargs):
        raise mod.ForgetError("找不到")

    monkeypatch.setattr(mod, "forget_event", boom)
    r = client.post(
        "/admin/forget", json={"event_id": "evt_missing"}, headers=AUTH
    )
    assert r.status_code == 404


# ---------------- B5 打标复核队列（A24：与 NEED_REVIEW 分开）----------------


@pytest.fixture()
def label_client(tmp_path, monkeypatch) -> TestClient:
    """打标复核队列用独立 SQLite，不碰真实数据。"""
    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = TOKEN
    db = (tmp_path / "label.sqlite3").as_posix()
    settings.DATABASE_URL = f"sqlite:///{db}"

    # /admin/review 会实时扫 Weaviate，这里给空集，只测「两条队列互不串味」
    import memory.entrypoints.admin_http as mod

    monkeypatch.setattr(mod, "_load_all", lambda cfg, ns: [])

    app = create_app(settings)
    app.state.label_db_url = settings.DATABASE_URL
    return TestClient(app)


def _seed_label(client: TestClient, event_id: str = "evt_" + "a" * 16) -> None:
    from memory.services.label_review import LabelReviewQueue

    url = client.app.state.label_db_url
    queue = LabelReviewQueue(url.split("///", 1)[-1])
    queue.push(
        namespace="work",
        event_id=event_id,
        summary="张三决定项目A延期到六月",
        rules_types=["work"],
        llm_types=["decision", "work"],
        agreement=0.5,
    )


def test_label_review_requires_token(label_client: TestClient) -> None:
    assert label_client.get("/admin/label-review").status_code == 401


def test_label_review_empty(label_client: TestClient) -> None:
    r = label_client.get("/admin/label-review", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0}


def test_label_review_lists_pending(label_client: TestClient) -> None:
    _seed_label(label_client)
    r = label_client.get("/admin/label-review", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["rules_types"] == ["work"]
    assert item["llm_types"] == ["decision", "work"]
    assert item["agreement"] == 0.5
    # 关键：这个响应里**没有** bucket / idle_days —— 那是 NEED_REVIEW 的字段
    assert "bucket" not in item
    assert "idle_days" not in item


def test_label_review_resolve(label_client: TestClient) -> None:
    eid = "evt_" + "a" * 16
    _seed_label(label_client, eid)
    r = label_client.post(
        f"/admin/label-review/{eid}/resolve",
        json={"resolution": "kept_rules"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json() == {"event_id": eid, "resolved": True, "resolution": "kept_rules"}

    # 处理完就不在待办里了
    assert label_client.get("/admin/label-review", headers=AUTH).json()["total"] == 0


def test_label_review_resolve_missing_is_404(label_client: TestClient) -> None:
    r = label_client.post(
        "/admin/label-review/evt_nope/resolve", json={}, headers=AUTH
    )
    assert r.status_code == 404


def test_label_review_resolve_twice_is_404(label_client: TestClient) -> None:
    eid = "evt_" + "a" * 16
    _seed_label(label_client, eid)
    assert label_client.post(
        f"/admin/label-review/{eid}/resolve", json={}, headers=AUTH
    ).status_code == 200
    assert label_client.post(
        f"/admin/label-review/{eid}/resolve", json={}, headers=AUTH
    ).status_code == 404


def test_label_review_two_queues_are_separate(label_client: TestClient) -> None:
    """A24：LABEL_REVIEW 不能出现在 NEED_REVIEW 的 bucket，反之亦然。"""
    _seed_label(label_client)

    # NEED_REVIEW 队列看不到打标分歧
    r = label_client.get("/admin/review?bucket=NEED_REVIEW", headers=AUTH)
    assert r.status_code == 200
    ids = {i["event_id"] for i in r.json()["items"]}
    assert "evt_" + "a" * 16 not in ids

    # LABEL_REVIEW 队列返回的条目没有 bucket 字段
    label_items = label_client.get("/admin/label-review", headers=AUTH).json()["items"]
    assert label_items
    assert all("bucket" not in i for i in label_items)


def test_label_review_limit(label_client: TestClient) -> None:
    for i in range(5):
        _seed_label(label_client, f"evt_{i:016x}")
    r = label_client.get("/admin/label-review?limit=2", headers=AUTH)
    assert len(r.json()["items"]) == 2
    assert r.json()["total"] == 5



# ---------------- D5 NEED_REVIEW 裁决（resolve）----------------


class FakeVerdictStore:
    """假 Weaviate：把裁决写回共享状态，好让 `_load_all` 下次读到新值。"""

    state: dict = {}
    calls: list[dict] = []

    def __init__(self, settings=None) -> None:
        pass

    def set_review_verdict(
        self, event_id, namespace, *, verdict, reviewer=None, pinned=None
    ):
        FakeVerdictStore.calls.append(
            {
                "event_id": event_id,
                "namespace": namespace,
                "verdict": verdict,
                "reviewer": reviewer,
                "pinned": pinned,
            }
        )
        row = FakeVerdictStore.state.setdefault(event_id, {})
        row["review_verdict"] = verdict
        if pinned is not None:
            row["pinned"] = pinned

    def close(self) -> None:
        pass


@pytest.fixture()
def resolve_client(monkeypatch) -> TestClient:
    import memory.entrypoints.admin_http as mod

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = TOKEN
    settings.FORGET_IDLE_DAYS = 180
    settings.FORGET_GRACE_DAYS = 90
    settings.FORGET_MIN_RECALLS = 1
    settings.PROTECT_IMPORTANCE_REVIEW = 0.7

    old = date.today() - timedelta(days=400)
    # importance 高 → NEED_REVIEW（灰区，要人看）
    rows = [
        {
            "event_id": "evt_review_me",
            "importance": 0.95,
            "recall_count": 0,
            "date_start": old.isoformat(),
            "retention": "normal",
            "pinned": False,
            "unresolved": [],
            "summary": "很重要但没人用过",
        },
        {
            "event_id": "evt_pinned_one",
            "importance": 0.95,
            "recall_count": 0,
            "date_start": old.isoformat(),
            "retention": "normal",
            "pinned": True,
            "unresolved": [],
            "summary": "被钉住的重要事",
        },
    ]
    FakeVerdictStore.calls = []
    FakeVerdictStore.state = {}

    def _load(cfg, ns):
        """把假 store 里记的裁决叠加到基础数据上，模拟真库读回。"""
        merged = []
        for r in rows:
            row = dict(r)
            row.update(FakeVerdictStore.state.get(r["event_id"], {}))
            merged.append(row)
        return _to_candidates(merged)

    monkeypatch.setattr(mod, "_load_all", _load)
    monkeypatch.setattr(mod, "WeaviateStore", FakeVerdictStore)
    return TestClient(create_app(settings))


def test_resolve_requires_token(resolve_client: TestClient) -> None:
    r = resolve_client.post(
        "/admin/review/evt_review_me/resolve", json={"action": "keep"}
    )
    assert r.status_code == 401


def test_resolve_keep_moves_to_protected(resolve_client: TestClient) -> None:
    r = resolve_client.post(
        "/admin/review/evt_review_me/resolve",
        json={"action": "keep", "reviewer": "owner"},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bucket_before"] == "NEED_REVIEW"
    assert body["bucket_after"] == "PROTECTED"
    assert body["deleted"] is False
    assert body["unchanged"] is False

    # keep 必须同时置 pinned，否则下次 sweep 又回到 NEED_REVIEW
    call = FakeVerdictStore.calls[-1]
    assert call["verdict"] == "keep"
    assert call["pinned"] is True
    assert call["reviewer"] == "owner"


def test_resolve_delete_moves_to_safe_but_not_deleted(resolve_client: TestClient) -> None:
    r = resolve_client.post(
        "/admin/review/evt_review_me/resolve",
        json={"action": "delete"},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bucket_after"] == "SAFE_TO_DELETE"
    assert body["deleted"] is False, "delete 只归档，不立即删（ADR 4.6）"

    call = FakeVerdictStore.calls[-1]
    assert call["verdict"] == "delete"
    assert call["pinned"] is None, "delete 不该动 pinned"


def test_resolve_delete_cannot_override_pinned(resolve_client: TestClient) -> None:
    """人工 delete 不推翻硬保护 —— 一次误点不该删掉 pinned 的卡。"""
    r = resolve_client.post(
        "/admin/review/evt_pinned_one/resolve",
        json={"action": "delete"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["bucket_after"] == "PROTECTED"


def test_resolve_missing_event_is_404(resolve_client: TestClient) -> None:
    r = resolve_client.post(
        "/admin/review/evt_nope/resolve", json={"action": "keep"}, headers=AUTH
    )
    assert r.status_code == 404


def test_resolve_bad_action_is_422(resolve_client: TestClient) -> None:
    r = resolve_client.post(
        "/admin/review/evt_review_me/resolve",
        json={"action": "destroy"},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_resolve_twice_is_idempotent(resolve_client: TestClient) -> None:
    body = {"action": "keep"}
    first = resolve_client.post(
        "/admin/review/evt_review_me/resolve", json=body, headers=AUTH
    )
    assert first.json()["unchanged"] is False
    # 同一 action 再来一次：仍然 200，但 unchanged=true
    second = resolve_client.post(
        "/admin/review/evt_review_me/resolve", json=body, headers=AUTH
    )
    assert second.status_code == 200
    assert second.json()["unchanged"] is True
    assert second.json()["bucket_after"] == "PROTECTED"


def test_resolve_opposite_action_allowed(resolve_client: TestClient) -> None:
    """人会改主意，改主意不该被拒绝。"""
    resolve_client.post(
        "/admin/review/evt_review_me/resolve", json={"action": "keep"}, headers=AUTH
    )
    r = resolve_client.post(
        "/admin/review/evt_review_me/resolve", json={"action": "delete"}, headers=AUTH
    )
    assert r.status_code == 200
    assert r.json()["unchanged"] is False


# ---------------- 别名拒绝留痕 + 姓氏声明（002）----------------
#
# 这两组接口是「为什么那条别名边没建」的查询入口。判据永远会有盲区，
# 所以重点不是断言判得对，而是断言**被拒的东西查得到、裁决得了**。


@pytest.fixture()
def alias_client(tmp_path, monkeypatch) -> TestClient:
    """留痕表用独立 SQLite，不碰真实数据。"""
    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = TOKEN
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'alias.sqlite3').as_posix()}"
    app = create_app(settings)
    app.state.alias_db_url = settings.DATABASE_URL
    return TestClient(app)


def _seed_reject(
    client: TestClient,
    *,
    event_id: str = "evt_" + "b" * 16,
    alias: str = "三哥主打的",
    canonical: str = "有声有色有味",
    side: str = "alias",
    reason: str = "ending",
    detail: str = "的",
) -> None:
    from memory.services.alias_reject import AliasRejectQueue

    queue = AliasRejectQueue(
        client.app.state.alias_db_url.split("///", 1)[-1]
    )
    queue.record_many(
        "work",
        event_id,
        [
            {
                "alias": alias,
                "canonical": canonical,
                "side": side,
                "reason": reason,
                "detail": detail,
            }
        ],
        source_text="原话",
    )


def test_alias_rejects_requires_token(alias_client: TestClient) -> None:
    assert alias_client.get("/admin/alias-rejects").status_code == 401


def test_alias_rejects_empty(alias_client: TestClient) -> None:
    r = alias_client.get("/admin/alias-rejects", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "by_reason": {}}


def test_alias_rejects_lists_and_groups(alias_client: TestClient) -> None:
    _seed_reject(alias_client)
    _seed_reject(
        alias_client,
        event_id="evt_" + "c" * 16,
        alias="糖包",
        canonical="这种人",
        side="canonical",
        reason="marker",
        detail="这种",
    )

    body = alias_client.get("/admin/alias-rejects", headers=AUTH).json()
    assert body["total"] == 2
    # `by_reason` 是最有用的一栏：某类突然涨起来通常意味着判据误伤了一整类
    assert body["by_reason"] == {"ending": 1, "marker": 1}
    assert {i["side"] for i in body["items"]} == {"alias", "canonical"}


def test_alias_rejects_filter_by_reason(alias_client: TestClient) -> None:
    _seed_reject(alias_client)
    _seed_reject(
        alias_client,
        event_id="evt_" + "c" * 16,
        alias="糖包",
        canonical="这种人",
        side="canonical",
        reason="marker",
        detail="这种",
    )
    body = alias_client.get(
        "/admin/alias-rejects", params={"reason": "marker"}, headers=AUTH
    ).json()
    assert body["total"] == 1
    assert body["items"][0]["alias"] == "糖包"
    # 过滤只影响 items/total，不影响全局分布
    assert body["by_reason"] == {"ending": 1, "marker": 1}


def test_alias_rejects_resolve(alias_client: TestClient) -> None:
    """`dismissed` 只改状态、不碰图（`accepted` 会补边，见 003 的测试）。"""
    eid = "evt_" + "b" * 16
    _seed_reject(alias_client, event_id=eid)

    r = alias_client.post(
        "/admin/alias-rejects/resolve",
        json={
            "event_id": eid,
            "alias": "三哥主打的",
            "canonical": "有声有色有味",
            "side": "alias",
            "resolution": "dismissed",
        },
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json() == {
        "resolved": True,
        "resolution": "dismissed",
        "repaired": None,
    }
    assert alias_client.get("/admin/alias-rejects", headers=AUTH).json()["total"] == 0


def test_alias_rejects_resolve_missing_is_404(alias_client: TestClient) -> None:
    r = alias_client.post(
        "/admin/alias-rejects/resolve",
        json={
            "event_id": "evt_nope",
            "alias": "a",
            "canonical": "b",
            "side": "alias",
        },
        headers=AUTH,
    )
    assert r.status_code == 404


def test_alias_surnames_requires_token(alias_client: TestClient) -> None:
    assert alias_client.get("/admin/alias-surnames").status_code == 401


def test_alias_surnames_declare_then_list(alias_client: TestClient) -> None:
    from memory.services.labeler import declared_surnames

    r = alias_client.post(
        "/admin/alias-surnames",
        json={"chars": ["种"], "evidence": "这人姓种"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert [i["char"] for i in r.json()["items"]] == ["种"]
    # 立即在**本进程**生效 —— 判据只认这个集合
    assert "种" in declared_surnames()
    assert "种" in r.json()["active"]

    body = alias_client.get("/admin/alias-surnames", headers=AUTH).json()
    assert [i["char"] for i in body["items"]] == ["种"]
    assert body["items"][0]["evidence"] == "这人姓种"


# ---------------- 003 · 裁决后真的补边 + 纠错痕迹可查 ----------------


def _patch_repair(monkeypatch, *, ok: bool, calls: list | None = None):
    import memory.services.alias_apply as AA

    def fake(**kw):
        if calls is not None:
            calls.append(kw)
        return ok

    monkeypatch.setattr(AA, "repair_accepted_edge", fake)


def test_resolve_accepted_repairs_edge(
    alias_client: TestClient, monkeypatch
) -> None:
    """accepted = 判据错了 → 图上真的补出边，`repaired=true`。"""
    _seed_reject(alias_client)
    calls: list = []
    _patch_repair(monkeypatch, ok=True, calls=calls)

    r = alias_client.post(
        "/admin/alias-rejects/resolve",
        json={
            "event_id": "evt_" + "b" * 16,
            "alias": "三哥主打的",
            "canonical": "有声有色有味",
            "side": "alias",
            "resolution": "accepted",
        },
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"resolved": True, "resolution": "accepted", "repaired": True}
    assert calls, "补建函数没被调用"
    assert calls[0]["alias"] == "三哥主打的"
    assert calls[0]["canonical"] == "有声有色有味"


def test_resolve_accepted_graph_down_marks_pending(
    alias_client: TestClient, monkeypatch
) -> None:
    """图写失败 → 记 `accept_pending`，**不假装成功**，且仍在待办里。"""
    _seed_reject(alias_client)
    _patch_repair(monkeypatch, ok=False)

    r = alias_client.post(
        "/admin/alias-rejects/resolve",
        json={
            "event_id": "evt_" + "b" * 16,
            "alias": "三哥主打的",
            "canonical": "有声有色有味",
            "side": "alias",
            "resolution": "accepted",
        },
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["repaired"] is False
    assert r.json()["resolution"] == "accept_pending"

    # 关键：它没有从待办里消失
    pending = alias_client.get("/admin/alias-rejects", headers=AUTH).json()
    assert pending["total"] == 1
    assert pending["items"][0]["resolution"] == "accept_pending"


def test_resolve_dismissed_does_not_repair(
    alias_client: TestClient, monkeypatch
) -> None:
    """dismissed = 判据对 → 不碰图，`repaired` 为 null。"""
    _seed_reject(alias_client)
    calls: list = []
    _patch_repair(monkeypatch, ok=True, calls=calls)

    r = alias_client.post(
        "/admin/alias-rejects/resolve",
        json={
            "event_id": "evt_" + "b" * 16,
            "alias": "三哥主打的",
            "canonical": "有声有色有味",
            "side": "alias",
            "resolution": "dismissed",
        },
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["repaired"] is None
    assert calls == []


def test_entity_edits_requires_token(alias_client: TestClient) -> None:
    assert alias_client.get("/admin/entities/edits").status_code == 401


def test_entity_edits_lists(
    alias_client: TestClient, monkeypatch
) -> None:
    """纠错痕迹可查 —— 光有写接口不算数。"""
    import memory.services.entity_edit as EE

    seen: dict = {}

    def fake_list(namespace, *, settings, op=None, target=None, limit=50, driver=None):
        seen.update(namespace=namespace, op=op, target=target, limit=limit)
        return {
            "items": [
                {
                    "op": "rename",
                    "target": "助手",
                    "args": {"from": "助手", "to": "豆包"},
                    "reason": "抽错了",
                    "operator": "admin",
                    "created_at": "2026-11-03T00:00:00+00:00",
                }
            ],
            "total": 1,
        }

    monkeypatch.setattr(EE, "list_edits", fake_list)

    r = alias_client.get(
        "/admin/entities/edits",
        params={"op": "rename", "target": "助手", "limit": 9},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["op"] == "rename"
    assert body["items"][0]["args"] == {"from": "助手", "to": "豆包"}
    assert body["items"][0]["args_parse_error"] is False
    assert seen["op"] == "rename"
    assert seen["target"] == "助手"
    assert seen["limit"] == 9

