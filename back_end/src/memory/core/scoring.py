"""C3 时效与重要性加权（ADR B1 的打分公式）。

    final = rel × (W_TIME_BASE + (1-W_TIME_BASE) × freshness)
              × (W_IMP_BASE  + (1-W_IMP_BASE)  × importance)

    freshness = 2 ** (-Δdays / RECENCY_HALF_LIFE_DAYS)

设计意图：**基座加权而非乘性归零**。`W_TIME_BASE=0.7` 表示即使一条记忆
再旧，也保留 70% 的相关性分——时效是「衰减」不是「否决」。
否则陈年但关键的记忆（比如三年前的合同条款）会被永久埋掉。
"""

from __future__ import annotations

#: 时效基座：即使 freshness=0，也保留 70% 相关性
DEFAULT_W_TIME_BASE = 0.7
#: 重要性基座：即使 importance=0，也保留 80%
DEFAULT_W_IMP_BASE = 0.8
#: 时效半衰期（天）
DEFAULT_HALF_LIFE_DAYS = 180


def freshness(delta_days: float, *, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """新鲜度 ∈ (0, 1]，半衰期后降到 0.5。

    Args:
        delta_days: 距今多少天；未来时间（负数）按 0 处理，不给额外奖励。
        half_life_days: 半衰期。
    """
    if half_life_days <= 0:
        raise ValueError(f"half_life_days 必须为正：{half_life_days}")
    age = max(0.0, float(delta_days))
    return 2.0 ** (-age / float(half_life_days))


def weighted_score(
    relevance: float,
    *,
    delta_days: float,
    importance: float,
    w_time_base: float = DEFAULT_W_TIME_BASE,
    w_imp_base: float = DEFAULT_W_IMP_BASE,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """合成最终分。

    Args:
        relevance: 相关性（rerank 或 RRF 分，建议先归一到 0–1）。
        delta_days: 事件结束（或开始）距今的天数。
        importance: 卡片重要度 0–1。
        w_time_base: 时效基座。
        w_imp_base: 重要性基座。
        half_life_days: 半衰期。

    Returns:
        final 分；因两个基座都 < 1，结果自然 ≤ relevance。
    """
    for name, value in (("w_time_base", w_time_base), ("w_imp_base", w_imp_base)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} 必须在 [0,1]：{value}")
    if not 0.0 <= importance <= 1.0:
        raise ValueError(f"importance 必须在 [0,1]：{importance}")

    fresh = freshness(delta_days, half_life_days=half_life_days)
    time_factor = w_time_base + (1.0 - w_time_base) * fresh
    imp_factor = w_imp_base + (1.0 - w_imp_base) * importance
    return float(relevance) * time_factor * imp_factor


def normalize_relevance(scores: list[float]) -> list[float]:
    """把任意正分线到 0–1，供 `weighted_score` 使用。

    全零或空列表返回全 0；分母用最大值，保持相对差距放大而非压缩。
    """
    if not scores:
        return []
    top = max(scores)
    if top <= 0:
        return [0.0 for _ in scores]
    return [max(0.0, s) / top for s in scores]
