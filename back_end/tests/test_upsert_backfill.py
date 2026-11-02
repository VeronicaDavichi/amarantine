"""upsert 语义：重放输入可以修正事实，不能改写历史。

`event_id` 是内容哈希，同一条事件重写永远拿同一个 id —— 所以
`POST /v1/objects` 返回 "already exists" 是**常态**，不是异常。

早先这条分支直接 return，导致给 schema 加新字段后老对象永远补不上。
修法是已存在时**只回填缺失的内容字段**。

核心约束：运行时字段（`recall_count` / `is_deleted` / `retention` …）
**绝不能**被重复上报改写 —— 那会让「这条记忆被用过 5 次」变成 0，
或者让一张删掉的卡自己复活。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import httpx
import pytest

from memory.config import Settings
from memory.models.event_card import EventCard
from memory.services.weaviate_store import (
    _BACKFILL_FIELDS,
    COLLECTION,
    WeaviateStore,
    to_properties,
)


def _settings() -> Settings:
    return Settings(
        MEMORY_NAMESPACE="work",
        WEAVIATE_URL="http://weaviate.test",
        NEO4J_URI="bolt://neo4j.test:7687",
        EMBEDDING_DIM=4,
    )


def _card(**overrides: Any) -> EventCard:
    kwargs: dict[str, Any] = dict(
        namespace="work",
        event_id="evt_00000000000000aa",
        date_start=date(2026, 6, 15),
        types=["work"],
        entities=[],
        summary="张三决定项目A延期到六月",
        retention="normal",
        ingested_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
        importance=0.5,
    )
    kwargs.update(overrides)
    return EventCard(**kwargs)


class Recorder:
    """记录所有请求，按脚本返回响应。"""

    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.existing = existing
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = None
        self.requests.append((request.method, str(request.url), body))

        # ensure_collection 的探测
        if request.method == "GET" and request.url.path == "/v1/schema":
            return httpx.Response(200, json={"classes": [{"class": COLLECTION}]})
        if request.method == "GET" and request.url.path == f"/v1/schema/{COLLECTION}":
            return httpx.Response(200, json={"properties": []})
        # POST 创建 → 一律说「已存在」
        if request.method == "POST" and request.url.path == "/v1/objects":
            return httpx.Response(
                422, text='{"error":[{"message":"id already exists"}]}'
            )
        # GET 读对象 → 返回既有状态
        if request.method == "GET" and request.url.path.startswith("/v1/objects/"):
            if self.existing is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"properties": self.existing})
        # PATCH 回填
        if request.method == "PATCH":
            return httpx.Response(204)
        return httpx.Response(200, json={})


def _store(recorder: Recorder) -> WeaviateStore:
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return WeaviateStore(_settings(), client=client)


# ---------------- 回填缺失字段 ----------------

def test_missing_field_is_backfilled() -> None:
    """老对象没有 `alias_claims` —— 重写时要补上。

    这是本 bug 的直接场景：给 schema 加字段后，老卡永远拿不到新字段。
    """
    rec = Recorder(existing={"namespace": "work", "event_id": "evt_00000000000000aa"})
    store = _store(rec)
    card = _card(alias_claims=[{"alias": "小张", "canonical": "张三", "label": "exact", "source": "user_declared"}])  # type: ignore[arg-type]

    store.insert_event(card, [0.1, 0.2, 0.3, 0.4])

    patches = [r for r in rec.requests if r[0] == "PATCH"]
    assert patches, "应该发出 PATCH 回填"
    sent = patches[0][2]["properties"]
    assert "alias_claims" in sent
    assert "小张" in sent["alias_claims"]


def test_existing_value_is_not_overwritten() -> None:
    """已有值不覆盖 —— 事实层不能由一次重复上报推翻。"""
    rec = Recorder(
        existing={
            "namespace": "work",
            "event_id": "evt_00000000000000aa",
            "summary": "用户说的原话",
        }
    )
    store = _store(rec)
    store.insert_event(_card(summary="被改写的话"), [0.1, 0.2, 0.3, 0.4])

    patches = [r for r in rec.requests if r[0] == "PATCH"]
    if patches:
        assert "summary" not in patches[0][2]["properties"]


def test_nothing_to_backfill_skips_patch() -> None:
    """所有字段都齐了，不该白发一次 PATCH。"""
    card = _card()
    props = to_properties(card)
    rec = Recorder(existing=props)
    store = _store(rec)

    store.insert_event(card, [0.1, 0.2, 0.3, 0.4])

    assert not [r for r in rec.requests if r[0] == "PATCH"]


def test_null_value_counts_as_missing() -> None:
    """旧对象值显式为 None 也算缺失（旧版本写的时候还是空）。"""
    rec = Recorder(
        existing={
            "namespace": "work",
            "event_id": "evt_00000000000000aa",
            "summary": None,
        }
    )
    store = _store(rec)
    store.insert_event(_card(), [0.1, 0.2, 0.3, 0.4])

    patches = [r for r in rec.requests if r[0] == "PATCH"]
    assert patches and "summary" in patches[0][2]["properties"]


# ---------------- 运行时字段必须被保护 ----------------

@pytest.mark.parametrize(
    "field",
    [
        "recall_count",
        "confirmed_by_use",
        "is_deleted",
        "retention",
        "pinned",
        "review_verdict",
        "reviewer",
        "ingested_at",
    ],
)
def test_runtime_fields_are_never_backfilled(field: str) -> None:
    """这些字段记录的是卡片的**当前状态**，重放输入不能改写历史。

    `recall_count` 被重置成 0 → 记忆用过的证据没了。
    `is_deleted` 被重置成 False → 删掉的卡自己复活了。
    """
    assert field not in _BACKFILL_FIELDS


def test_recall_count_preserved_through_reinsert() -> None:
    """重报同一条内容，`recall_count` 必须原样保留。"""
    rec = Recorder(
        existing={
            "namespace": "work",
            "event_id": "evt_00000000000000aa",
            "recall_count": 7,
            "confirmed_by_use": 3,
        }
    )
    store = _store(rec)
    store.insert_event(_card(), [0.1, 0.2, 0.3, 0.4])

    patches = [r for r in rec.requests if r[0] == "PATCH"]
    for _, _, body in patches:
        assert "recall_count" not in (body or {}).get("properties", {})
        assert "confirmed_by_use" not in (body or {}).get("properties", {})


def test_deleted_card_does_not_resurrect() -> None:
    """删掉的卡重报同一条内容不能自己复活。"""
    rec = Recorder(
        existing={
            "namespace": "work",
            "event_id": "evt_00000000000000aa",
            "is_deleted": True,
        }
    )
    store = _store(rec)
    store.insert_event(_card(), [0.1, 0.2, 0.3, 0.4])

    patches = [r for r in rec.requests if r[0] == "PATCH"]
    for _, _, body in patches:
        assert "is_deleted" not in (body or {}).get("properties", {})


# ---------------- 失败不影响主流程 ----------------

def test_backfill_failure_does_not_raise() -> None:
    """回填是增强，失败了主流程（幂等成功）已经达成，不该抛异常。"""

    class Boom(Recorder):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.startswith("/v1/objects/"):
                raise httpx.ConnectError("boom", request=request)
            return super().__call__(request)

    rec = Boom()
    store = _store(rec)
    # 不抛
    uid = store.insert_event(_card(), [0.1, 0.2, 0.3, 0.4])
    assert uid


def test_patch_non_2xx_does_not_raise() -> None:
    class BadPatch(Recorder):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PATCH":
                return httpx.Response(500, text="nope")
            return super().__call__(request)

    store = _store(BadPatch(existing={"namespace": "work"}))
    store.insert_event(_card(), [0.1, 0.2, 0.3, 0.4])  # 不抛


# ---------------- 创建路径不受影响 ----------------

def test_fresh_create_does_not_patch() -> None:
    """新建对象走 POST 成功路径，不该触发回填。"""

    class Created(Recorder):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/objects":
                return httpx.Response(200, json={"id": "new"})
            return super().__call__(request)

    rec = Created()
    store = _store(rec)
    store.insert_event(_card(), [0.1, 0.2, 0.3, 0.4])
    assert not [r for r in rec.requests if r[0] == "PATCH"]


# ---------------- remember 的幂等短路也要回填 ----------------

def test_remember_idempotent_path_backfills() -> None:
    """`remember` 的幂等检查在 `insert_event` **之前**短路。

    这意味着 `insert_event` 里的 422 分支在正常流程中根本走不到 ——
    如果只在 store 层修，schema 升级后老对象永远补不上（这是真踩过的坑）。
    所以 `remember` 的短路分支必须自己调一次 `backfill_event`。
    """
    from memory.services.remember import remember_event

    calls: list[str] = []

    class FakeStore:
        """只模拟「这条已经存在」这一个事实。"""

        def ensure_collection(self) -> None:
            pass

        def get_by_event_id(self, namespace: str, event_id: str):
            return {"id": "uid", "is_deleted": False, "event_id": event_id}

        def backfill_event(self, card: Any, *, anomaly: str | None = None) -> int:
            calls.append(card.event_id)
            return 1

        def revive(self, *a: Any, **kw: Any) -> None:
            raise AssertionError("没删过，不该复活")

        def insert_event(self, *a: Any, **kw: Any) -> str:
            raise AssertionError("已存在时不该再走 insert_event")

    result = remember_event(
        namespace="work",
        summary="小张也叫张三",
        settings=_settings(),
        store=FakeStore(),  # type: ignore[arg-type]
        embedder=lambda text, s: [0.0, 0.0, 0.0, 0.0],
        graph_writer=lambda *a, **kw: None,
    )

    assert result.created is False, "幂等短路应报告 created=False"
    assert calls, "remember 的幂等短路分支没有调用 backfill_event"
    assert calls[0].startswith("evt_")


def test_remember_idempotent_path_survives_missing_backfill_api() -> None:
    """老 store 实现没有 `backfill_event` 时不能崩 —— 用 getattr 探测。"""
    from memory.services.remember import remember_event

    class OldStore:
        def ensure_collection(self) -> None:
            pass

        def get_by_event_id(self, namespace: str, event_id: str):
            return {"id": "uid", "is_deleted": False}

        def insert_event(self, *a: Any, **kw: Any) -> str:
            raise AssertionError("不该走 insert_event")

    result = remember_event(
        namespace="work",
        summary="小张也叫张三",
        settings=_settings(),
        store=OldStore(),  # type: ignore[arg-type]
        embedder=lambda text, s: [0.0, 0.0, 0.0, 0.0],
        graph_writer=lambda *a, **kw: None,
    )
    assert result.created is False
