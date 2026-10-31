"""C2 RRF 融合单测。

RRF 的每个性质都关系到召回正确性，所以逐条钉住：
名次越大贡献越小、多路命中累加、同路重复不刷分、排序确定可复现。
"""

from __future__ import annotations

import pytest

from memory.core.rrf import DEFAULT_K, fuse_with_channels, rrf_fuse


def test_two_channels_fuse_and_sum() -> None:
    """同时出现在两路里的 id 分数应高于只在单路的。"""
    ranked = rrf_fuse(
        {
            "vector": ["a", "b"],
            "graph": ["a", "c"],
        }
    )
    scores = dict(ranked)
    assert scores["a"] > scores["b"]
    assert scores["a"] > scores["c"]
    assert scores["b"] == pytest.approx(scores["c"])  # 同为该路第 2 名


def test_first_place_beats_second() -> None:
    ranked = rrf_fuse({"vector": ["first", "second"]})
    assert [eid for eid, _ in ranked] == ["first", "second"]


def test_duplicate_in_same_channel_not_double_counted() -> None:
    """同一路里重复的 id 只按最好名次计一次，否则可以被刷分。"""
    ranked = rrf_fuse({"vector": ["a", "a", "a", "b"]})
    scores = dict(ranked)
    single = dict(rrf_fuse({"vector": ["a", "b"]}))
    assert scores["a"] == pytest.approx(single["a"])
    assert scores["b"] == pytest.approx(single["b"])


def test_larger_k_flattens_scores() -> None:
    """k 越大，名次间分差越小（原论文用 60 的用意）。"""
    small_k = dict(rrf_fuse({"v": ["a", "b"]}, k=1))
    large_k = dict(rrf_fuse({"v": ["a", "b"]}, k=1000))
    assert (small_k["a"] - small_k["b"]) > (large_k["a"] - large_k["b"])


def test_empty_channels_returns_empty() -> None:
    assert rrf_fuse({}) == []
    assert rrf_fuse({"vector": []}) == []


def test_failed_channel_is_simply_removed() -> None:
    """ADR C.B4：某路失败直接移除，不补偿其他路。"""
    with_graph = dict(rrf_fuse({"vector": ["a"], "graph": ["a"]}))
    without_graph = dict(rrf_fuse({"vector": ["a"]}))
    assert with_graph["a"] > without_graph["a"]
    # 移除 graph 后，vector 的分值本身不应被放大
    assert without_graph["a"] == pytest.approx(1.0 / (DEFAULT_K + 1))


def test_zero_weight_channel_is_skipped() -> None:
    ranked = rrf_fuse({"vector": ["a"], "graph": ["a"]}, weights={"graph": 0.0})
    assert dict(ranked)["a"] == pytest.approx(1.0 / (DEFAULT_K + 1))


def test_ties_are_deterministic() -> None:
    """同分必须按 id 排序，否则多次调用结果会漂移。"""
    a = [eid for eid, _ in rrf_fuse({"x": ["b"], "y": ["a"]})]
    b = [eid for eid, _ in rrf_fuse({"y": ["a"], "x": ["b"]})]
    assert a == b == ["a", "b"]


def test_non_positive_k_rejected() -> None:
    with pytest.raises(ValueError, match="k 必须为正"):
        rrf_fuse({"v": ["a"]}, k=0)


def test_diagnostics_shape() -> None:
    out = fuse_with_channels({"vector": ["a", "b"], "graph": ["c"]})
    assert out["k"] == DEFAULT_K
    assert out["channel_sizes"] == {"vector": 2, "graph": 1}
    assert len(out["ranked"]) == 3
