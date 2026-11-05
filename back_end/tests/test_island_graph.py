"""spec 022 图查询层：`event_neighborhood` 的语义。

重点测「图不可用返回 None」而不是空字典 —— 两者对孤岛判定的后果完全相反：
None → 不判孤岛；{} → 全员孤岛 → 全库加速淘汰。
"""

from __future__ import annotations

from memory.services.graph_search import EventNeighborhood, event_neighborhood


class _Rows(list):
    """neo4j Result 的最小替身：可迭代 + `.single()`。"""

    def single(self):
        return self[0] if self else None


class _FakeSession:
    def __init__(self, responses: dict[str, list], boom: bool = False) -> None:
        self._responses = responses
        self._boom = boom

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False

    def run(self, cypher: str, **kwargs):
        if self._boom:
            raise RuntimeError("neo4j down")
        for marker, rows in self._responses.items():
            if marker in cypher:
                return _Rows(rows)
        return _Rows([])


class _FakeDriver:
    def __init__(self, responses: dict[str, list], boom: bool = False) -> None:
        self._responses = responses
        self._boom = boom
        self.closed = False

    def session(self):
        return _FakeSession(self._responses, boom=self._boom)

    def close(self) -> None:
        self.closed = True


def _driver(neighbors, total, boom=False):
    return _FakeDriver(
        {
            "neighbor_events": neighbors,   # 邻域查询的 RETURN 列名
            "count(e)": total,              # 总数查询
        },
        boom=boom,
    )


def test_returns_neighbors_and_total() -> None:
    drv = _driver(
        [{"event_id": "e1", "neighbor_events": 0}, {"event_id": "e2", "neighbor_events": 3}],
        [{"total": 120}],
    )
    got = event_neighborhood(["e1", "e2"], "work", settings=None, driver=drv)
    assert got is not None
    assert got.neighbors == {"e1": 0, "e2": 3}
    assert got.total_events == 120


def test_none_when_graph_unavailable() -> None:
    """**最关键的一条**：图挂了必须 None，不能是 {}。

    返回 {} 的话，调用方会算出「每个事件 0 个邻居」= 全员孤岛，
    于是一次图抖动就会把整库记忆加速淘汰。
    """
    drv = _driver([], [], boom=True)
    got = event_neighborhood(["e1"], "work", settings=None, driver=drv)
    assert got is None, "图不可用时返回了空结构，调用方会误判全员孤岛"


def test_missing_event_id_is_absent_not_zero() -> None:
    """没查到的事件不在字典里，由调用方决定怎么处理（当前是按 0）。"""
    drv = _driver([{"event_id": "e1", "neighbor_events": 2}], [{"total": 10}])
    got = event_neighborhood(["e1", "e_missing"], "work", settings=None, driver=drv)
    assert got is not None
    assert "e_missing" not in got.neighbors
    assert got.neighbors.get("e_missing", 0) == 0


def test_empty_input_short_circuits() -> None:
    got = event_neighborhood([], "work", settings=None)
    assert isinstance(got, EventNeighborhood)
    assert got.neighbors == {}
    assert got.total_events == 0


def test_does_not_close_injected_driver() -> None:
    """注入的 driver 由调用方负责关闭 —— 关错了会打断别人的会话。"""
    drv = _driver([{"event_id": "e1", "neighbor_events": 1}], [{"total": 5}])
    event_neighborhood(["e1"], "work", settings=None, driver=drv)
    assert drv.closed is False


def test_total_zero_when_event_nodes_absent() -> None:
    """namespace 里没有 Event 节点 → 总数 0 → 数据量门槛会挡住孤岛判定。"""
    drv = _driver([], [])
    got = event_neighborhood(["e1"], "work", settings=None, driver=drv)
    assert got is not None
    assert got.total_events == 0
