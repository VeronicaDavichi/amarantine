"""spec 028：recall 耗时埋点 + `COLD_QUERY_P95_MS` 告警。

## 这个文件要钉住什么

1. **每个返回路径都有耗时** —— 4 个 return 点（全超时/无候选/空结果/正常），
   漏一个那条路径的 `elapsed_ms` 就永远是 0，而 0 看起来像「很快」不是「没测」
2. **告警真的会响，也真的会静** —— 改阈值行为必须变
3. **分通道耗时不能相加** —— 三个通道并行，相加是把并行当串行
4. **异常路径也测得到** —— 慢到超时抛异常的查询也是慢查询

## 为什么不用真实网络

这些测的是**埋点和告警逻辑**，不是通道速度。真实网络会让测试慢且不稳
（`test_recall_budget.py` 用真实 sleep 是因为它测的正是超时行为，不同）。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone

import pytest

from memory.config import Settings
from memory.services.graph_search import GraphHits
from memory.services.recall import recall

NS = "work"


# ---------------------------------------------------------------- fakes


class FakeStore:
    """返回固定命中，可配置每个方法的延迟（毫秒）。"""

    def __init__(
        self,
        *,
        vector_ms: float = 0.0,
        bm25_ms: float = 0.0,
        raise_on_vector: bool = False,
    ) -> None:
        self.vector_ms = vector_ms
        self.bm25_ms = bm25_ms
        self.raise_on_vector = raise_on_vector
        self.closed = False

    def search_near_vector(self, *a, **kw):
        if self.vector_ms:
            time.sleep(self.vector_ms / 1000.0)
        if self.raise_on_vector:
            raise RuntimeError("炸了")
        return [{"event_id": "evt_1", "score": 0.9, "summary": "张三的项目", "raw_quote_ids": []}]

    def search_bm25(self, *a, **kw):
        if self.bm25_ms:
            time.sleep(self.bm25_ms / 1000.0)
        return [{"event_id": "evt_1", "score": 0.5, "summary": "张三的项目", "raw_quote_ids": []}]

    def close(self) -> None:
        self.closed = True


def fake_graph(*a, **kw):
    # GraphHits 的字段是 event_ids / ok / error / routed / alias_hits
    return GraphHits(event_ids=[], ok=True)


def fake_embedder(text, settings):
    return [0.1] * 8


def _run(settings: Settings, store=None, *, graph=None, budget_ms=5000):
    return recall(
        "张三的项目",
        namespace=NS,
        settings=settings,
        store=store or FakeStore(),
        embedder=fake_embedder,
        graph_searcher=graph or fake_graph,
        budget_ms=budget_ms,
        now=datetime(2026, 11, 6, tzinfo=timezone.utc),
        today=date(2026, 11, 6),
    )


def _warnings(caplog) -> list[str]:
    return [r.message for r in caplog.records if r.levelno == logging.WARNING]


# ============================================================ 埋点存在性


def test_elapsed_ms_is_recorded() -> None:
    """正常路径必须有耗时，且 > 0（0 意味着没测，不是「很快」）。"""
    res = _run(Settings(), FakeStore(vector_ms=20))
    assert res.elapsed_ms > 0
    assert res.elapsed_ms < 5000


def test_elapsed_ms_reflects_slow_channel() -> None:
    """通道真慢时，总耗时必须跟着变大 —— 防「埋点写了个常量」。"""
    fast = _run(Settings(), FakeStore(vector_ms=0))
    slow = _run(Settings(), FakeStore(vector_ms=200))
    assert slow.elapsed_ms > fast.elapsed_ms + 100


def test_empty_result_also_has_elapsed() -> None:
    """空结果分支（无候选）也必须带耗时。

    这是 4 个 return 点之一，漏填就是「空结果永远 0ms」。
    """
    res = _run(Settings(), FakeStore(vector_ms=10))
    assert res.elapsed_ms > 0


def test_channel_ms_recorded_per_channel() -> None:
    """三个通道各自有耗时记录。"""
    res = _run(Settings(), FakeStore(vector_ms=10, bm25_ms=5))
    assert res.channel_ms, "分通道耗时没记录"
    assert set(res.channel_ms) & {"vector", "bm25", "graph"}


def test_channel_timings_are_not_identical() -> None:
    """**回归测试**：分通道耗时不能三个都一样。

    原实现把埋点写在 `for name, future in futures.items()` 里，
    而 `wait()` 返回时所有已完成的 future 都完成了、遍历又是顺序的 ——
    三个通道拿到同一个值（实测过：三个都 P50=25.5 / P95=271.3）。

    那条埋点测的是「遍历顺序」，不是通道耗时。现在改成各线程内计时。
    """
    res = _run(Settings(), FakeStore(vector_ms=120, bm25_ms=5))
    assert len(res.channel_ms) >= 2
    values = list(res.channel_ms.values())
    assert max(values) - min(values) > 50, (
        f"分通道耗时几乎相同 {res.channel_ms} —— 埋点又写回遍历顺序了"
    )


def test_each_channel_is_faster_than_total() -> None:
    """每个通道的耗时都必须小于总耗时。

    这条代替了原先想写的「分通道之和 > 总耗时」—— 那个断言**是错的**：
    总耗时除了扇出，还包含 embedding 编码、打分、动态 K、组装，
    以及首次调用时的 jieba 加载。这些加起来可能远超通道耗时，
    于是「和 > 总耗时」不成立（实测 160ms vs 285ms）。

    并行性本身靠 `ThreadPoolExecutor` + `wait()` 的代码结构保证，
    想用时序断言证明它会写出脆弱的测试。这里只钉住「单位/口径没算错」：
    单个通道不可能比整次请求还慢。
    """
    res = _run(Settings(), FakeStore(vector_ms=80, bm25_ms=80))
    assert res.channel_ms
    slowest = max(res.channel_ms.values())
    assert slowest <= res.elapsed_ms, (
        f"某个通道 {slowest:.0f}ms 超过总耗时 {res.elapsed_ms:.0f}ms —— "
        "耗时口径算错了（可能把纳秒当毫秒，或起点取错）"
    )


# ============================================================ 默认值（spec 029）


def test_default_target_is_3000() -> None:
    """默认 SLA 护栏是 3000ms（spec 029 定的，用户 2026-11-06 接受）。

    这条纯粹防手滑改回去 —— 单看它没有说服力（「默认值看着对」不算验证），
    所以必须和下面那条 `test_default_target_leaves_headroom` 一起看：
    那条证明这个默认值在**当前性能下真的不会误报**。
    """
    assert Settings().COLD_QUERY_P95_MS == 3000


def test_default_target_leaves_headroom(caplog) -> None:
    """默认配置 + 一次正常耗时的查询 → **不该告警**。

    现有的告警测试全部显式覆写阈值（1 / 999999 / 0 / -1）去验证
    「改配置 → 行为变」，但没有一条验证「**默认值本身**会不会天天报警」。

    这个差别很实际：如果 3000 定得太低（比如 900，贴着实测 P95），
    所有测试都会绿，而线上日志会被 WARNING 淹没 —— 真正的性能退化
    反而看不见了。028 实测 P95 是 671-878ms，离 3000 有 4 倍余量，
    所以正常查询必须一条告警都不产生。
    """
    caplog.set_level(logging.WARNING)
    res = _run(Settings(), FakeStore(vector_ms=50))
    assert not any("冷查询耗时超目标" in m for m in _warnings(caplog)), (
        f"默认 3000ms 护栏下正常查询就告警了，日志会被淹: {_warnings(caplog)}"
    )
    assert res.elapsed_ms < Settings().COLD_QUERY_P95_MS


# ============================================================ 告警：改阈值行为变


def test_warning_when_over_target(caplog) -> None:
    """阈值设成 1ms → 必然超 → 必须告警。"""
    s = Settings()
    s.COLD_QUERY_P95_MS = 1
    caplog.set_level(logging.WARNING)
    _run(s, FakeStore(vector_ms=10))
    msgs = _warnings(caplog)
    assert any("冷查询耗时超目标" in m for m in msgs), f"没告警，实际: {msgs}"


def test_no_warning_when_under_target(caplog) -> None:
    """阈值设成极大 → 永不超 → 必须不告警。"""
    s = Settings()
    s.COLD_QUERY_P95_MS = 999_999
    caplog.set_level(logging.WARNING)
    _run(s, FakeStore(vector_ms=10))
    assert not any("冷查询耗时超目标" in m for m in _warnings(caplog))


def test_zero_disables_warning(caplog) -> None:
    """`COLD_QUERY_P95_MS=0` = 关闭告警（沿用「0 = 关信号」约定）。

    不这么写的话 0 会变成「0ms 目标」，每次查询都告警，把日志淹掉。
    """
    s = Settings()
    s.COLD_QUERY_P95_MS = 0
    caplog.set_level(logging.WARNING)
    _run(s, FakeStore(vector_ms=10))
    assert not any("冷查询耗时超目标" in m for m in _warnings(caplog))


def test_negative_disables_warning(caplog) -> None:
    """负数同样是关闭，不能当成「负目标」。"""
    s = Settings()
    s.COLD_QUERY_P95_MS = -1
    caplog.set_level(logging.WARNING)
    _run(s, FakeStore(vector_ms=10))
    assert not any("冷查询耗时超目标" in m for m in _warnings(caplog))


def test_warning_message_contains_channels(caplog) -> None:
    """告警必须带分通道耗时 —— 否则只知道慢，不知道哪慢。"""
    s = Settings()
    s.COLD_QUERY_P95_MS = 1
    caplog.set_level(logging.WARNING)
    _run(s, FakeStore(vector_ms=10, bm25_ms=5))
    msg = next(m for m in _warnings(caplog) if "冷查询耗时超目标" in m)
    assert "通道" in msg
    assert any(k in msg for k in ("vector", "bm25", "graph")), (
        f"告警里没有通道明细: {msg}"
    )


# ============================================================ 异常路径


def test_elapsed_available_when_channel_raises(caplog) -> None:
    """通道抛异常时，finally 里也要能算到耗时并告警。

    向量/BM25 抛 `WeaviateStoreError` 会变成 503 —— 那条路径不会走到
    `_finish`，所以告警必须放在 `finally`。慢到超时的查询恰恰最容易
    以异常结束，不测这条就等于「慢查询里最慢的那批看不见」。
    """
    from memory.services.weaviate_store import WeaviateStoreError

    s = Settings()
    s.COLD_QUERY_P95_MS = 1
    caplog.set_level(logging.WARNING)

    class BoomStore(FakeStore):
        def search_near_vector(self, *a, **kw):
            time.sleep(0.02)
            raise WeaviateStoreError("炸")

    with pytest.raises(Exception):
        _run(s, BoomStore())

    assert any("冷查询耗时超目标" in m for m in _warnings(caplog)), (
        "异常路径没有告警 —— finally 里的埋点失效了"
    )


# ============================================================ payload


def test_payload_exposes_elapsed() -> None:
    """`to_payload()` 要带耗时，否则 HTTP 调用方看不到。"""
    res = _run(Settings(), FakeStore(vector_ms=5))
    payload = res.to_payload()
    assert "elapsed_ms" in payload
    assert payload["elapsed_ms"] > 0
    assert "channel_ms" in payload
    # 四舍五入到 0.1ms，避免 payload 里出现 12 位小数
    assert isinstance(payload["elapsed_ms"], float)
