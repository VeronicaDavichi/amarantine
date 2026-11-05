"""spec 022 孤岛降权。

核心不是测代码，是测**三重安全阀**：默认关闭、数据量门槛、访问历史合取。
缺任何一条，这个功能在冷启动阶段都会变成「全员加速淘汰」。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from memory.core.forget_policy import (
    BUCKET_PROTECTED,
    BUCKET_SAFE_TO_DELETE,
    ForgetCandidate,
    classify,
    sweep,
)

TODAY = date(2026, 11, 5)

#: 满足「淘汰条件」的基线卡：老、没被召回、retention normal
BASE = dict(
    event_id="evt_x",
    created_at=TODAY - timedelta(days=200),
    last_recalled_at=TODAY - timedelta(days=100),
    recall_count=0,
    retention="normal",
)


def make(**over) -> ForgetCandidate:
    data = dict(BASE)
    data.update(over)
    return ForgetCandidate(**data)


def run(candidate: ForgetCandidate, **over):
    kwargs = dict(
        today=TODAY,
        idle_days=90,
        min_recalls=2,
        grace_days=7,
        protect_importance=0.9,
        graph_hub_degree=8,
        confirmed_min=2,
        island_max_neighbors=0,
        island_min_events=50,
        namespace_events=100,
    )
    kwargs.update(over)
    return classify(candidate, **kwargs)


# ---------------- D3（最重要）：默认关闭 ----------------

def test_disabled_by_default_changes_nothing() -> None:
    """`island_max_neighbors=0`（默认）时，孤岛逻辑完全不生效。

    这条是回归保护：加功能不能改变默认行为。
    """
    island_card = make(neighbor_events=0)      # 看起来是孤岛
    normal_card = make(event_id="evt_y", neighbor_events=50)

    # 默认参数下，两者判定必须一致（都不因孤岛而加速）
    assert run(island_card).bucket == run(normal_card).bucket

    # 而且走的是**原有**淘汰逻辑：idle=100 >= 90 本来就该淘汰，
    # 但 reason 必须是 idle_and_unrecalled，不能是 island_and_unrecalled。
    got = run(island_card, idle_days=90)
    assert got.bucket == BUCKET_SAFE_TO_DELETE
    assert got.reason == "idle_and_unrecalled"


# ---------------- D1 / D2：孤岛判定 ----------------

def test_zero_neighbors_is_island_and_halves_threshold() -> None:
    """邻居 0 + 数据量够 → 孤岛 → idle 门槛减半。

    idle=100 天，门槛 90 天。非孤岛不淘汰；孤岛门槛变 45 天，所以淘汰。
    """
    card = make(neighbor_events=0)
    got = run(card, island_max_neighbors=2, idle_days=90)
    assert got.bucket == BUCKET_SAFE_TO_DELETE
    assert got.reason == "island_and_unrecalled"


def test_many_neighbors_is_not_island() -> None:
    """邻居多 → 不是孤岛 → 保持原门槛 → 不淘汰（idle 100 < 90 不成立？）。"""
    card = make(neighbor_events=5, last_recalled_at=TODAY - timedelta(days=100))
    got = run(card, island_max_neighbors=2, idle_days=90)
    assert got.reason != "island_and_unrecalled"
    # idle=100 >= 90，非孤岛也满足淘汰条件 —— 所以这里要验证的是 reason 不同
    assert got.bucket == BUCKET_SAFE_TO_DELETE


def test_island_boundary_is_inclusive() -> None:
    """`neighbor_events == island_max_neighbors` 边界算孤岛（<=）。"""
    card = make(neighbor_events=2)
    got = run(card, island_max_neighbors=2, idle_days=90)
    assert got.reason == "island_and_unrecalled"


# ---------------- D4：数据量门槛 ----------------

def test_small_namespace_is_never_island() -> None:
    """数据量不够时，邻居 0 也不算孤岛。

    冷启动阶段每条记忆都是孤立的，那不是孤岛，是还没积累。
    """
    card = make(neighbor_events=0)
    got = run(
        card,
        island_max_neighbors=2,
        island_min_events=50,
        namespace_events=3,          # 只有 3 条
        idle_days=90,
    )
    assert got.reason != "island_and_unrecalled"


def test_namespace_at_threshold_is_enough() -> None:
    """正好等于门槛时启用（>=）。"""
    card = make(neighbor_events=0)
    got = run(
        card,
        island_max_neighbors=2,
        island_min_events=50,
        namespace_events=50,
        idle_days=90,
    )
    assert got.reason == "island_and_unrecalled"


# ---------------- D5：图不可用 ----------------

def test_graph_unavailable_is_never_island() -> None:
    """`neighbor_events=None` = 图不可用 → 不判孤岛。

    如果把 None 当成 0，图抖动那一刻全库都会被加速淘汰。
    """
    card = make(neighbor_events=None)
    got = run(card, island_max_neighbors=2, idle_days=90)
    assert got.reason != "island_and_unrecalled"


def test_graph_unavailable_survives_sweep() -> None:
    """sweep 全量 + 全部 neighbor_events=None → 没有一个被判孤岛。"""
    cards = [make(event_id=f"evt_{i}", neighbor_events=None) for i in range(5)]
    report = sweep(
        cards,
        today=TODAY,
        idle_days=90,
        min_recalls=2,
        grace_days=7,
        protect_importance=0.9,
        island_max_neighbors=2,
        island_min_events=50,
        namespace_events=100,
    )
    assert all(v.reason != "island_and_unrecalled" for v in report.verdicts)


# ---------------- D6：访问历史合取 ----------------

def test_island_with_many_recalls_is_not_accelerated() -> None:
    """孤岛但被反复召回 → 不加速。

    「婚礼」可能只有 2-3 个节点，但会被查很多次。访问历史比孤岛更能说明有用。
    """
    card = make(neighbor_events=0, recall_count=10)
    got = run(card, island_max_neighbors=2, idle_days=90)
    assert got.bucket != BUCKET_SAFE_TO_DELETE


def test_island_recently_recalled_is_not_accelerated() -> None:
    """孤岛但最近才被召回 → idle 小 → 门槛减半也不够。"""
    card = make(neighbor_events=0, last_recalled_at=TODAY - timedelta(days=10))
    got = run(card, island_max_neighbors=2, idle_days=90)
    assert got.bucket != BUCKET_SAFE_TO_DELETE


# ---------------- D7 / D8：门槛减半的效果与控制组 ----------------

@pytest.mark.parametrize("idle", [46, 70, 89])
def test_island_halves_threshold_effect(idle: int) -> None:
    """idle 在 [45, 90) 区间：孤岛淘汰，非孤岛保留。"""
    island_card = make(
        event_id="a", neighbor_events=0, last_recalled_at=TODAY - timedelta(days=idle)
    )
    normal_card = make(
        event_id="b", neighbor_events=9, last_recalled_at=TODAY - timedelta(days=idle)
    )
    assert (
        run(island_card, island_max_neighbors=2, idle_days=90).bucket
        == BUCKET_SAFE_TO_DELETE
    )
    assert run(normal_card, island_max_neighbors=2, idle_days=90).bucket != BUCKET_SAFE_TO_DELETE


def test_island_does_not_affect_below_halved_line() -> None:
    """idle 远低于减半后的线（比如 20 天 < 45）→ 孤岛也不淘汰。"""
    card = make(neighbor_events=0, last_recalled_at=TODAY - timedelta(days=20))
    got = run(card, island_max_neighbors=2, idle_days=90)
    assert got.bucket != BUCKET_SAFE_TO_DELETE


# ---------------- D9：不绕过任何硬信号 ----------------

@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"pinned": True}, "pinned 是绝对保护"),
        ({"retention": "permanent"}, "永久保留"),
        ({"unresolved": ["x"]}, "有未决事项"),
        ({"review_verdict": "keep"}, "人工判过 keep"),
    ],
)
def test_island_never_bypasses_hard_signals(over: dict, why: str) -> None:
    """孤岛+极老+零召回，仍然被硬信号保护。孤岛只是加速器，不是删除理由。"""
    card = make(neighbor_events=0, **over)
    got = run(card, island_max_neighbors=2, idle_days=90)
    assert got.bucket == BUCKET_PROTECTED, why


def test_island_does_not_bypass_hub_protection() -> None:
    """即使邻居少，degree 达到 hub 阈值仍然保护（两条信号独立）。"""
    card = make(neighbor_events=0, graph_degree=8)
    got = run(card, island_max_neighbors=2, idle_days=90, graph_hub_degree=8)
    assert got.bucket == BUCKET_PROTECTED


# ---------------- 参数校验 ----------------

def test_negative_island_params_rejected() -> None:
    with pytest.raises(ValueError):
        run(make(), island_max_neighbors=-1)
    with pytest.raises(ValueError):
        run(make(), island_min_events=-1)


# ---------------- 边界：idle_days // 2 不能把门槛归零 ----------------
#
# 这组是修完 bug 后补的。原来 `idle_days // 2` 在 idle_days=1 时得 0，
# 门槛归零后 `idle >= 0` 永远成立 —— 连**刚刚被召回过的**孤岛卡都会
# 被判 SAFE_TO_DELETE。实测复现过，不是理论风险。

def test_tiny_idle_days_does_not_zero_out_the_threshold() -> None:
    """`idle_days=1` + 孤岛 → 刚召回过的卡**不能**被淘汰。"""
    card = make(
        neighbor_events=0,
        last_recalled_at=TODAY,           # idle = 0
        created_at=TODAY - timedelta(days=300),
    )
    got = run(card, island_max_neighbors=2, idle_days=1)
    assert got.bucket != BUCKET_SAFE_TO_DELETE, (
        "门槛被 1//2=0 归零了，刚召回过的卡也会被判淘汰"
    )


def test_tiny_idle_days_with_one_day_idle_is_still_eligible() -> None:
    """`idle_days=1` 时，闲置 1 天的孤岛卡**应该**淘汰（门槛 floor 到 1）。"""
    card = make(
        neighbor_events=0,
        last_recalled_at=TODAY - timedelta(days=1),
        created_at=TODAY - timedelta(days=300),
    )
    got = run(card, island_max_neighbors=2, idle_days=1)
    assert got.bucket == BUCKET_SAFE_TO_DELETE


def test_idle_days_zero_keeps_existing_semantics() -> None:
    """`idle_days=0` 是既有的「立即」语义，孤岛不改变它。

    ADR 没定义 0 的含义，这里只保证「孤岛没有偷偷改它」——
    真要重新定义 0，得单独议。
    """
    card = make(neighbor_events=0, last_recalled_at=TODAY, created_at=TODAY - timedelta(days=300))
    got = run(card, island_max_neighbors=2, idle_days=0)
    # idle=0 >= 0 成立 -> 淘汰（既有行为）
    assert got.bucket == BUCKET_SAFE_TO_DELETE


def test_two_days_halves_to_one() -> None:
    """`idle_days=2` → 孤岛门槛 1，闲置 1 天即淘汰。"""
    card = make(
        neighbor_events=0,
        last_recalled_at=TODAY - timedelta(days=1),
        created_at=TODAY - timedelta(days=300),
    )
    got = run(card, island_max_neighbors=2, idle_days=2)
    assert got.reason == "island_and_unrecalled"
