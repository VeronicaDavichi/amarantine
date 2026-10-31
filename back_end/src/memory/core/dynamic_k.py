"""C4 动态 top-K 决策。

ADR D3 的立场：**K 是结果，不是入参**。外部只给 token 预算，
条数由分数分布与预算共同决定。六步规则（顺序即优先级）：

    1. 强制保留  pinned / unresolved 命中项，不参与截断，允许溢出
    2. 绝对过滤  score < MIN_SCORE 直接丢弃
    3. 相对衰减  score < max_score × DROP_RATIO 丢弃
    4. 断崖检测  降序相邻落差 > GAP_RATIO 处截断
    5. token 预算 累计超 CONTEXT_TOKEN_BUDGET 处截断
    6. 条数夹取  clamp(K, MIN, MAX)

优先级：**强制保留 > 绝对过滤 > 预算 > 条数夹取**（ADR 原文）。
第 1 条可突破上限，此时 `overflow=true` 必须上报 —— 记忆安全高于 context 整洁。

一个刻意的取舍：**下限不补低分**。若过滤后只剩 3 条而 min_k=5，
本模块不会拉两条垃圾凑数（那正是 ADR 禁止的「低分材料填充」），
只标记 `clamp_min` 让调用方知道候选不足。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """参与截断决策的候选。`forced` 由上游按 D7 硬信号标记。"""

    event_id: str
    score: float
    tokens: int
    forced: bool = False


@dataclass(frozen=True)
class KDecision:
    """截断结果与归因。`reason` 进 `trace.top_k_decision`，必须可解释。"""

    selected: list[str]
    k: int
    reason: str
    overflow: bool
    dropped_min_score: int
    dropped_drop_ratio: int
    dropped_gap: int
    dropped_budget: int
    dropped_clamp: int

    def to_trace(self) -> dict[str, object]:
        return {
            "k": self.k,
            "reason": self.reason,
            "overflow": self.overflow,
            "dropped": {
                "min_score": self.dropped_min_score,
                "drop_ratio": self.dropped_drop_ratio,
                "gap": self.dropped_gap,
                "budget": self.dropped_budget,
                "clamp": self.dropped_clamp,
            },
        }


def decide_k(
    candidates: list[Candidate],
    *,
    budget_tokens: int,
    min_k: int,
    max_k: int,
    min_score: float,
    drop_ratio: float,
    gap_ratio: float,
) -> KDecision:
    """执行六步截断。

    Args:
        candidates: 已打分的候选（顺序不限，本函数自己排）。
        budget_tokens: 材料集 token 上限。
        min_k: 条数下限（**不补低分**，仅用于归因）。
        max_k: 条数上限。
        min_score: 绝对分门槛。
        drop_ratio: 相对衰减比例，如 0.35 表示低于最高分 35% 的丢弃。
        gap_ratio: 断崖比例，如 0.4 表示相邻降幅超 40% 处截断。

    Returns:
        KDecision。
    """
    if min_k < 0 or max_k < 0:
        raise ValueError("min_k / max_k 不能为负")
    if min_k > max_k:
        raise ValueError(f"min_k({min_k}) 不能大于 max_k({max_k})")
    if budget_tokens < 0:
        raise ValueError(f"budget_tokens 不能为负：{budget_tokens}")
    if not 0.0 <= drop_ratio <= 1.0:
        raise ValueError(f"drop_ratio 必须在 [0,1]：{drop_ratio}")
    if not 0.0 <= gap_ratio <= 1.0:
        raise ValueError(f"gap_ratio 必须在 [0,1]：{gap_ratio}")

    if not candidates:
        return KDecision([], 0, "empty", False, 0, 0, 0, 0, 0)

    # 1) 强制保留项先分离出来，它们不受 2–5 步影响
    forced = sorted(
        (c for c in candidates if c.forced), key=lambda c: (-c.score, c.event_id)
    )
    normal = sorted(
        (c for c in candidates if not c.forced), key=lambda c: (-c.score, c.event_id)
    )

    # 2) 绝对过滤
    after_min = [c for c in normal if c.score >= min_score]
    dropped_min = len(normal) - len(after_min)

    # 3) 相对衰减
    dropped_ratio = 0
    if after_min:
        threshold = after_min[0].score * drop_ratio
        kept = [c for c in after_min if c.score >= threshold]
        dropped_ratio = len(after_min) - len(kept)
        after_min = kept

    # 4) 断崖检测：在降序序列里第一次大幅落差之后全部截断
    gap_cut_at: int | None = None
    for i in range(1, len(after_min)):
        prev = after_min[i - 1].score
        if prev <= 0:
            continue
        if (prev - after_min[i].score) / prev > gap_ratio:
            gap_cut_at = i
            break
    if gap_cut_at is not None:
        dropped_gap = len(after_min) - gap_cut_at
        after_min = after_min[:gap_cut_at]
    else:
        dropped_gap = 0

    # 5) token 预算（强制项已占用预算，仍可突破上限）
    used = sum(c.tokens for c in forced)
    selected_normal: list[Candidate] = []
    budget_cut = False
    for c in after_min:
        if used + c.tokens > budget_tokens:
            budget_cut = True
            break
        selected_normal.append(c)
        used += c.tokens
    dropped_budget = len(after_min) - len(selected_normal)

    # 6) 条数夹取：上限砍尾（强制项始终在最前，不会被砍掉）
    dropped_clamp = 0
    overflow = False
    merged = forced + selected_normal
    total = len(merged)
    if len(forced) > max_k:
        # 强制项自身就超上限：全留强制项，普通项不再加入
        # （若把普通项也放进来，溢出会无限膨胀；记忆安全只需保住强制项）
        overflow = True
        dropped_clamp = len(selected_normal)
        merged = forced
    elif total > max_k:
        dropped_clamp = total - max_k
        merged = merged[:max_k]

    k = len(merged)

    # 归因：按「最先发生、影响最大」的顺序取
    if overflow:
        reason = "overflow_forced"
    elif dropped_clamp > 0:
        reason = "clamp_max"
    elif budget_cut:
        reason = "budget"
    elif dropped_gap > 0:
        reason = "gap"
    elif dropped_ratio > 0:
        reason = "drop_ratio"
    elif dropped_min > 0 and not merged:
        reason = "min_score"
    elif k < min_k:
        reason = "clamp_min"
    else:
        reason = "all"

    return KDecision(
        selected=[c.event_id for c in merged],
        k=k,
        reason=reason,
        overflow=overflow,
        dropped_min_score=dropped_min,
        dropped_drop_ratio=dropped_ratio,
        dropped_gap=dropped_gap,
        dropped_budget=dropped_budget,
        dropped_clamp=dropped_clamp,
    )
