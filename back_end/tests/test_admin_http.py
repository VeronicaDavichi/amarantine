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
