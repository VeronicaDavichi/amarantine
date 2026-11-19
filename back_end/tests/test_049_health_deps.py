"""spec 049 G1：`/health` 汇总四依赖（活着 ≠ 依赖健康）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from memory.entrypoints import admin_http


def _client(settings) -> TestClient:
    app = admin_http.create_app(settings, maintenance=False)
    return TestClient(app)


def _settings():
    import pathlib

    from memory.config import load_settings

    s = load_settings()
    # 用临时库，避免测试碰真实记忆
    return s


def test_health_liveness_only(monkeypatch) -> None:
    """`deps=0` → 只报进程活着（探针用，不打外部服务）。"""
    c = _client(_settings())
    r = c.get("/health", params={"deps": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "deps" not in body


def test_health_reports_deps(monkeypatch) -> None:
    """默认带上四项依赖状态。"""
    import httpx

    class _Resp:
        status_code = 200

    def fake_get(url, timeout=None):
        return _Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(admin_http, "_DEP_CACHE", None)

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("socket.create_connection", lambda *a, **kw: _Sock())

    c = _client(_settings())
    body = c.get("/health").json()
    assert body["status"] == "ok"          # 进程活着
    assert body["degraded"] is False
    assert set(body["deps"]) == {"weaviate", "embedding", "rerank", "neo4j"}
    assert body["down"] == []


def test_degraded_when_dependency_down(monkeypatch) -> None:
    import httpx

    class _Bad:
        status_code = 503

    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Bad())
    monkeypatch.setattr("socket.create_connection",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("refused")))
    monkeypatch.setattr(admin_http, "_DEP_CACHE", None)

    c = _client(_settings())
    body = c.get("/health").json()
    # **关键**：status 仍是 ok（liveness 不该因依赖挂掉而重启容器）
    assert body["status"] == "ok"
    assert body["degraded"] is True
    assert set(body["down"]) == {"weaviate", "embedding", "rerank", "neo4j"}


def test_deps_cached(monkeypatch) -> None:
    """高频探针不能每次都打四个外部服务。"""
    import httpx

    calls = {"n": 0}

    class _Resp:
        status_code = 200

    def fake_get(url, timeout=None):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(httpx, "get", fake_get)

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("socket.create_connection", lambda *a, **kw: _Sock())
    monkeypatch.setattr(admin_http, "_DEP_CACHE", None)

    c = _client(_settings())
    c.get("/health")
    first = calls["n"]
    c.get("/health")
    assert calls["n"] == first, "第二次应命中缓存"
    assert c.get("/health").json().get("cached") is True
