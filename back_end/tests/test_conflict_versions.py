"""A6：同实体两版本事实必须都返回，不许合并。

场景：主人两次说项目A的进度 ——

- 2026-08「项目A延期到六月」
- 2026-10「项目A又延期到七月」

这是**两张卡**（event_id 不同），但 `entity_names` 里都有 `项目A`。
如果召回层把「同实体」当成同一件事去重，只会返回一条 ——
主人就看不到「变更」，而变更恰恰是记忆最该记住的东西。

**核实结论**：`rrf_fuse` 按 `event_id` 合并，`recall.py` 没有任何
按实体去重的路径。所以 A6 是「尚未被违反」，不是「已被实现」。
本文件的职责是把这条语义**钉死**，防止未来有人为了「减少重复」加一个
按实体折叠的优化。
"""

from __future__ import annotations

from tests.test_recall import (
    FakeStore,
    _hit,
    _run,
)


def _version(eid: str, *, days_ago: int, summary: str, entities: list[str]) -> dict:
    h = _hit(eid, summary, days_ago=days_ago, score=0.9)
    h["entity_names"] = entities
    return h


def _both_versions() -> tuple[dict, dict]:
    june = _version(
        "evt_june", days_ago=60, summary="项目A延期到六月", entities=["项目A", "六月"]
    )
    july = _version(
        "evt_july", days_ago=2, summary="项目A又延期到七月", entities=["项目A", "七月"]
    )
    return june, july


def _run_both():
    june, july = _both_versions()
    return _run(
        # 去掉 query 里的时间 hint —— 否则 date_range 过滤会把旧版本筛掉，
        # 那测的就变成「时间过滤」而不是「版本不合并」了。
        query="项目A的进度",
        store=FakeStore(vector_hits=[june, july], bm25_hits=[june, july]),
    )


# ---------------- D1：两条都要在 ----------------


def test_both_versions_are_returned() -> None:
    result = _run_both()
    ids = [m["event_id"] for m in result.materials]
    assert "evt_june" in ids, "旧版本被合并掉了 —— 看不到变更"
    assert "evt_july" in ids
    assert len(ids) == 2


def test_versions_are_not_the_same_card() -> None:
    """两条必须是两张不同的卡，不是同一张被返回两次。"""
    result = _run_both()
    ids = [m["event_id"] for m in result.materials]
    assert len(set(ids)) == len(ids)
    assert len(set(ids)) == 2


# ---------------- D2：各带自己的时间戳 ----------------


def test_versions_carry_their_own_timestamps() -> None:
    result = _run_both()
    dates = {m["event_id"]: m.get("date_start") for m in result.materials}

    assert dates["evt_june"], "旧版本丢了日期"
    assert dates["evt_july"], "新版本丢了日期"
    assert dates["evt_june"] != dates["evt_july"], "两版本日期相同，看不出先后"


# ---------------- D4：不相关实体不能被一起折叠 ----------------


def test_unrelated_entity_is_not_folded() -> None:
    june, july = _both_versions()
    other = _version("evt_b", days_ago=20, summary="项目B立项", entities=["项目B"])
    result = _run(
        query="项目A的进度",
        store=FakeStore(vector_hits=[june, july, other], bm25_hits=[june, july, other]),
    )
    assert len(result.materials) == 3, "三条不同实体的卡被折叠了"


# ---------------- 两版本对同一实体的贡献可解释 ----------------


def test_both_versions_keep_their_own_summary() -> None:
    """不许把两版本合成一个摘要 —— 那样「当时是怎么说的」就消失了。"""
    result = _run_both()
    summaries = {m["event_id"]: m["summary"] for m in result.materials}
    assert "六月" in summaries["evt_june"]
    assert "七月" in summaries["evt_july"]
