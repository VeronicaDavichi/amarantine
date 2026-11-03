"""A10：五类硬信号必须都生效，且各带独立 reason。

ADR D7 的五类：
1. `pinned`
2. `retention ∈ 永久保留`
3. `unresolved` 非空
4. 图结构位置（degree ≥ GRAPH_HUB_DEGREE）
5. `confirmed_by_use ≥ CONFIRMED_MIN`

第 4、5 类在这次之前是**完全没实现**的（config 有阈值，判定零引用）。
"""

from __future__ import annotations

from datetime import date

import pytest

from memory.core.forget_policy import (
    BUCKET_PROTECTED,
    BUCKET_SAFE_TO_DELETE,
    ForgetCandidate,
    classify,
)

TODAY = date(2026, 11, 3)
#: 老到足以进淘汰候选的日期（不触发宽限期）
OLD = date(2023, 1, 1)


def _verdict(c: ForgetCandidate, **overrides):
    kwargs = dict(
        today=TODAY,
        idle_days=180,
        min_recalls=1,
        grace_days=90,
        protect_importance=0.7,
    )
    kwargs.update(overrides)
    return classify(c, **kwargs)


# ---------------- D1 / D2：五类各一条，reason 互不相同 ----------------


@pytest.mark.parametrize(
    ("name", "candidate", "expected_reason"),
    [
        ("pinned", ForgetCandidate(event_id="e1", pinned=True, created_at=OLD), "pinned"),
        (
            "permanent_retention",
            ForgetCandidate(event_id="e2", retention="permanent", created_at=OLD),
            "retention=permanent",
        ),
        (
            "unresolved",
            ForgetCandidate(event_id="e3", unresolved=["等回复"], created_at=OLD),
            "unresolved",
        ),
        (
            "graph_hub",
            ForgetCandidate(event_id="e4", graph_degree=8, created_at=OLD),
            "graph_hub",
        ),
        (
            "confirmed_by_use",
            ForgetCandidate(event_id="e5", confirmed_by_use=2, created_at=OLD),
            "confirmed_by_use",
        ),
    ],
)
def test_each_hard_signal_protects(name, candidate, expected_reason) -> None:
    v = _verdict(candidate)
    assert v.bucket == BUCKET_PROTECTED, f"{name} 没被保护"
    assert v.reason == expected_reason, f"{name} 的 reason 不对: {v.reason}"


def test_all_five_reasons_are_distinct() -> None:
    """五类信号的 reason 必须两两不同 —— 否则周报里分不清是哪类保护。"""
    reasons = {
        _verdict(ForgetCandidate(event_id="e1", pinned=True, created_at=OLD)).reason,
        _verdict(ForgetCandidate(event_id="e2", retention="permanent", created_at=OLD)).reason,
        _verdict(ForgetCandidate(event_id="e3", unresolved=["x"], created_at=OLD)).reason,
        _verdict(ForgetCandidate(event_id="e4", graph_degree=8, created_at=OLD)).reason,
        _verdict(ForgetCandidate(event_id="e5", confirmed_by_use=2, created_at=OLD)).reason,
    }
    assert len(reasons) == 5, f"reason 撞了: {reasons}"


# ---------------- D3 / D4：边界必须二值可判 ----------------


def test_graph_degree_boundary() -> None:
    """degree=8 命中；=7 不命中。差一就是「保护 / 可删」的分界。"""
    hit = _verdict(ForgetCandidate(event_id="a", graph_degree=8, created_at=OLD))
    miss = _verdict(ForgetCandidate(event_id="b", graph_degree=7, created_at=OLD))
    assert hit.bucket == BUCKET_PROTECTED
    assert hit.reason == "graph_hub"
    assert miss.bucket != BUCKET_PROTECTED or miss.reason != "graph_hub"


def test_confirmed_by_use_boundary() -> None:
    hit = _verdict(ForgetCandidate(event_id="a", confirmed_by_use=2, created_at=OLD))
    miss = _verdict(ForgetCandidate(event_id="b", confirmed_by_use=1, created_at=OLD))
    assert hit.bucket == BUCKET_PROTECTED
    assert hit.reason == "confirmed_by_use"
    assert miss.bucket != BUCKET_PROTECTED or miss.reason != "confirmed_by_use"


# ---------------- 阈值 0 = 关闭这条信号，不是「全都保护」 ----------------


def test_zero_threshold_disables_the_signal() -> None:
    """配 0 意味着关掉，而不是「所有卡都 >= 0 于是全被保护」。

    这是个容易写反的地方：`degree >= 0` 恒真。
    """
    v = _verdict(
        ForgetCandidate(event_id="a", graph_degree=0, created_at=OLD),
        graph_hub_degree=0,
    )
    assert v.reason != "graph_hub", "阈值 0 竟然触发了保护"
    assert v.bucket == BUCKET_SAFE_TO_DELETE, "老卡本来就该进删除候选"


def test_zero_confirmed_threshold_disables() -> None:
    v = _verdict(
        ForgetCandidate(event_id="a", confirmed_by_use=0, created_at=OLD),
        confirmed_min=0,
    )
    assert v.reason != "confirmed_by_use"


# ---------------- D6：默认 0 不改变现有行为 ----------------


def test_defaults_do_not_change_behavior() -> None:
    """不传新字段时，分桶结果必须与实现前完全一致。"""
    v = _verdict(ForgetCandidate(event_id="a", created_at=OLD))
    assert v.bucket == BUCKET_SAFE_TO_DELETE
    assert v.reason == "idle_and_unrecalled"


def test_negative_thresholds_rejected() -> None:
    with pytest.raises(ValueError):
        _verdict(ForgetCandidate(event_id="a"), graph_hub_degree=-1)
    with pytest.raises(ValueError):
        _verdict(ForgetCandidate(event_id="a"), confirmed_min=-1)


# ---------------- D5：图查不到必须能区分「不可用」和「度为 0」 ----------------


class _FakeSession:
    def __init__(self, rows) -> None:
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False

    def run(self, cypher, **kwargs):
        return self._rows


class _FakeDriver:
    def __init__(self, rows=None, boom: bool = False) -> None:
        self._rows = rows or []
        self._boom = boom
        self.closed = False

    def session(self):
        if self._boom:
            raise RuntimeError("neo4j down")
        return _FakeSession(self._rows)

    def close(self) -> None:
        self.closed = True


def test_event_degrees_returns_map() -> None:
    from memory.services.graph_search import event_degrees

    rows = [{"event_id": "e1", "degree": 9}, {"event_id": "e2", "degree": 3}]
    out = event_degrees(["e1", "e2"], "work", settings=None, driver=_FakeDriver(rows))
    assert out == {"e1": 9, "e2": 3}


def test_event_degrees_returns_none_on_failure() -> None:
    """图挂了必须返回 None，不能返回 {} —— 两者语义完全不同。"""
    from memory.services.graph_search import event_degrees

    out = event_degrees(
        ["e1"], "work", settings=None, driver=_FakeDriver(boom=True)
    )
    assert out is None, "图不可用时返回了 {}，调用方会把「查不了」当成「度为 0」"


def test_event_degrees_empty_input_short_circuits() -> None:
    from memory.services.graph_search import event_degrees

    assert event_degrees([], "work", settings=None) == {}


def test_event_degrees_does_not_close_injected_driver() -> None:
    """注入的 driver 由调用方负责关闭 —— 关错了会打断别人的会话。"""
    from memory.services.graph_search import event_degrees

    drv = _FakeDriver([{"event_id": "e1", "degree": 1}])
    event_degrees(["e1"], "work", settings=None, driver=drv)
    assert drv.closed is False


# ---------------- D5（端点级）：降级要显式告诉调用方 ----------------


def test_sweep_marks_graph_degraded(monkeypatch, tmp_path) -> None:
    """图查不了时 sweep 必须仍 200，但响应带 `graph_degraded=true`。

    这条比「函数返回 None」更重要：如果端点把 None 静默当 0，
    报告看起来一切正常，实则「图枢纽」这整类保护没生效。
    """
    from fastapi.testclient import TestClient

    import memory.entrypoints.admin_http as mod
    from memory.config import Settings

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = "tok"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'sweep.db').as_posix()}"

    monkeypatch.setattr(mod, "_load_all", lambda cfg, ns: [])
    monkeypatch.setattr(mod, "event_degrees", lambda *a, **k: None)

    client = TestClient(mod.create_app(settings))
    r = client.post(
        "/admin/forget/sweep",
        json={"dry_run": True},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert r.json()["graph_degraded"] is True, "图不可用却没标降级"


def test_sweep_not_degraded_when_graph_ok(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    import memory.entrypoints.admin_http as mod
    from memory.config import Settings

    settings = Settings()
    settings.MEMORY_NAMESPACE = "work"
    settings.ADMIN_TOKEN = "tok"
    settings.DATABASE_URL = f"sqlite:///{(tmp_path / 'sweep2.db').as_posix()}"

    monkeypatch.setattr(mod, "_load_all", lambda cfg, ns: [])
    monkeypatch.setattr(mod, "event_degrees", lambda *a, **k: {})

    client = TestClient(mod.create_app(settings))
    r = client.post(
        "/admin/forget/sweep",
        json={"dry_run": True},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 200
    assert r.json()["graph_degraded"] is False
