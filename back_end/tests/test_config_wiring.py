"""014：三个配置项**真的生效**（不是只把字段接上就算）。

`test_config_usage.py` 只能证明「有人在代码里提到了这个字段」。
本文件证明「改了它的值，行为真的会变」—— 两者缺一不可：
一个字段可以被引用来打印日志，但对行为毫无影响。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from memory.config import Settings, load_settings
from memory.entrypoints.admin_http import create_app
from memory.services.graph_search import search_by_entities
from tests.test_recall import FakeStore, _hit
from tests.test_recall import _run as _recall_run
from tests.test_rerank import _FakeClient, _settings as _rerank_settings

TOKEN = "tok-014"


# ---------------- D1: RERANK_CANDIDATES 真的截断 ----------------

def _store_with(n: int) -> FakeStore:
    rows = [
        {
            "event_id": f"evt_{i}",
            "summary": f"第 {i} 条",
            "importance": 0.5,
            "date_start": (datetime.now(timezone.utc) - timedelta(days=i)).isoformat(),
            "recall_count": 0,
            "last_recalled_at": None,
            "retention": "normal",
            "pinned": False,
            "unresolved": [],
        }
        for i in range(n)
    ]
    hits = [_hit(r["event_id"] and f"evt_{i}") for i, r in enumerate(rows)]
    store = FakeStore(vector_hits=hits, bm25_hits=hits)
    store.list_all = lambda ns: rows  # type: ignore[method-assign]
    return store


def test_rerank_candidates_truncates_the_pool() -> None:
    """`RERANK_CANDIDATES=1` → 只有 1 条候选被送进 rerank。"""
    client = _FakeClient(data=[{"index": 0, "score": 0.9}])
    settings = _rerank_settings()
    settings.RERANK_ENABLED = True
    settings.RERANK_CANDIDATES = 1

    _recall_run(
        query="项目A的进度",
        settings=settings,
        store=_store_with(3),
        rerank_client=client,
    )

    assert client.calls, "压根没调 rerank"
    texts = client.calls[0]["json"]["texts"]
    assert len(texts) == 1, f"RERANK_CANDIDATES 没生效，送了 {len(texts)} 条"


def test_rerank_candidates_default_keeps_more() -> None:
    """默认 50 时不该截断到这 3 条以下 —— 否则上一测无法区分「生效」与「恒为 1」。"""
    client = _FakeClient(data=[{"index": i, "score": 0.5} for i in range(3)])
    settings = _rerank_settings()
    settings.RERANK_ENABLED = True
    settings.RERANK_CANDIDATES = 50

    _recall_run(
        query="项目A的进度",
        settings=settings,
        store=_store_with(3),
        rerank_client=client,
    )
    assert len(client.calls[0]["json"]["texts"]) == 3


# ---------------- D2: GRAPH_TIMEOUT_MS 真的传给了 Neo4j ----------------

class _Cfg:
    def __init__(self, ms: int) -> None:
        self.GRAPH_TIMEOUT_MS = ms


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher, **params):
        self.calls.append((cypher, params))

        class _R:
            def __iter__(self_inner):
                return iter([])

            def data(self_inner):
                return []

            def single(self_inner):
                return None

            def consume(self_inner):
                return None

        return _R()


class _Driver:
    def __init__(self) -> None:
        self.session_obj = _Session()

    def session(self):
        return self.session_obj

    def close(self):
        pass


@pytest.mark.parametrize("ms", [200, 1500])
def test_graph_timeout_comes_from_config(ms: int) -> None:
    drv = _Driver()
    search_by_entities(["项目A"], "work", settings=_Cfg(ms), driver=drv)
    assert drv.session_obj.calls, "没发查询"
    # 别名解析那次查询不带 timeout，主查询才带（见 SEARCH_CYPHER 的参数表）
    timeouts = [p["timeout"] for _, p in drv.session_obj.calls if "timeout" in p]
    assert timeouts, "没有任何一次查询带 timeout 参数"
    for actual in timeouts:
        assert actual == pytest.approx(ms / 1000.0), (
            f"GRAPH_TIMEOUT_MS={ms} 没生效，实际传给 Neo4j 的是 {actual}"
        )


def test_graph_timeout_explicit_arg_still_wins() -> None:
    """显式传参可覆盖配置（测试与特殊调用路径需要）。"""
    drv = _Driver()
    search_by_entities(["项目A"], "work", settings=_Cfg(9999), driver=drv, timeout_s=0.05)
    timeouts = [p["timeout"] for _, p in drv.session_obj.calls if "timeout" in p]
    assert timeouts and all(t == pytest.approx(0.05) for t in timeouts)


# ---------------- D3: AGENT_KEY_HEADER 真的决定读哪个 header ----------------

def _client(monkeypatch, header_name: str) -> TestClient:
    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = TOKEN
    settings.AGENT_KEY_HEADER = header_name

    import memory.entrypoints.admin_http as mod

    monkeypatch.setattr(mod, "_load_all", lambda cfg, ns: [])
    return TestClient(create_app(settings))


def test_authorization_requires_bearer_prefix(monkeypatch) -> None:
    c = _client(monkeypatch, "Authorization")
    ok = c.get("/admin/alias-rejects", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200, ok.text

    # 少了 Bearer 前缀 → 401（与旧实现一致）
    bad = c.get("/admin/alias-rejects", headers={"Authorization": TOKEN})
    assert bad.status_code == 401


def test_authorization_wrong_token_is_403(monkeypatch) -> None:
    c = _client(monkeypatch, "Authorization")
    r = c.get("/admin/alias-rejects", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_custom_header_uses_raw_value(monkeypatch) -> None:
    """非 Authorization 名字时，整个 header 值就是 token，不剥 Bearer。"""
    c = _client(monkeypatch, "X-Agent-Key")
    ok = c.get("/admin/alias-rejects", headers={"X-Agent-Key": TOKEN})
    assert ok.status_code == 200, ok.text


def test_custom_header_ignores_authorization(monkeypatch) -> None:
    """配了 X-Agent-Key 之后，Authorization 不再被接受（配置是唯一真值）。"""
    c = _client(monkeypatch, "X-Agent-Key")
    r = c.get("/admin/alias-rejects", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401


def test_missing_header_reports_the_configured_name(monkeypatch) -> None:
    """401 的报错要点出真正的 header 名，否则排查时会被误导。"""
    c = _client(monkeypatch, "X-Agent-Key")
    r = c.get("/admin/alias-rejects")
    assert r.status_code == 401
    assert "X-Agent-Key" in r.text


def test_default_header_is_authorization() -> None:
    """默认值必须是 Authorization —— 不能描述一个不存在的协议。"""
    assert load_settings().AGENT_KEY_HEADER == "Authorization"
