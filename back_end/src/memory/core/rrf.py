"""C2 RRF（Reciprocal Rank Fusion）融合。

ADR C.B4 的约定：

- **等权**，`k=60`（RRF 标准值），不引入手工权重
- 某通道失败 → **直接移除该路，不补偿、不放大其他路**
- 理由：RRF 的前提就是「不需要知道各路可靠度」。若某路挂了就把权重分给其他路，
  单通道故障会被放大成系统性偏差

`weights` 参数只为 P2 预留（观测到某路系统性偏噪时才用），默认全 1.0。
"""

from __future__ import annotations

#: RRF 平滑常数（越大越平缓；60 是原论文经验值）
DEFAULT_K = 60


def rrf_fuse(
    channels: dict[str, list[str]],
    *,
    k: int = DEFAULT_K,
    weights: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """把多路有序候选融合成统一的打分排序。

    Args:
        channels: ``{通道名: [event_id, ...]}``；列表顺序即该路的排名（越前越好）。
        k: RRF 平滑常数。
        weights: 可选的通道权重；缺省 1.0（ADR 默认等权）。

    Returns:
        ``[(event_id, score), ...]``，按 score 降序；同分按 event_id 升序，
        保证结果可复现（不依赖字典顺序）。

    Note:
        同一路里重复出现的 id 只按**最好名次**计一次，避免刷分。
    """
    if k <= 0:
        raise ValueError(f"k 必须为正：{k}")

    w = weights or {}
    scores: dict[str, float] = {}

    for name, ids in channels.items():
        weight = float(w.get(name, 1.0))
        if weight <= 0:
            # 权重为 0 等价于该路不参与，仍然计 0 分而不是报错
            continue
        # 先去重保序再编号：重复项是脏数据，不该连累后面合法项的名次
        unique_ids = list(dict.fromkeys(ids))
        for rank, event_id in enumerate(unique_ids, start=1):
            scores[event_id] = scores.get(event_id, 0.0) + weight / (k + rank)

    # 同分时按 id 升序，让排序完全确定
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def fuse_with_channels(
    channels: dict[str, list[str]],
    *,
    k: int = DEFAULT_K,
    weights: dict[str, float] | None = None,
) -> dict[str, object]:
    """带诊断信息的融合结果，供 `trace` 上报。

    Returns:
        ``{"ranked": [...], "channel_sizes": {...}, "k": 60}``
    """
    ranked = rrf_fuse(channels, k=k, weights=weights)
    return {
        "ranked": ranked,
        "channel_sizes": {name: len(ids) for name, ids in channels.items()},
        "k": k,
    }
