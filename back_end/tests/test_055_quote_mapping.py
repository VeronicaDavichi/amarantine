"""spec 055：每张卡只挂自己的原文（按 source_indices），不是整批窗口。"""

from __future__ import annotations

import pytest

from memory.services.raw_quote import quotes_for_event


class _Ev:
    def __init__(self, indices) -> None:
        self.source_indices = indices


QIDS = [f"msg_{i:02d}" for i in range(1, 31)]  # 30 条窗口


def test_picks_only_own_quotes() -> None:
    """一张卡只该挂它 source_indices 指的几条。"""
    got = quotes_for_event(_Ev([3, 4, 5]), QIDS, 30)
    assert got == ["msg_03", "msg_04", "msg_05"]


def test_one_based_indexing() -> None:
    """source_indices 是 1-based —— 传 1 应拿到第 0 个。"""
    assert quotes_for_event(_Ev([1]), QIDS, 30) == ["msg_01"]


def test_out_of_range_ignored() -> None:
    got = quotes_for_event(_Ev([0, 1, 31, 999]), QIDS, 30)
    assert got == ["msg_01"], "越界索引跳过，不报错，也不崩溃"


def test_dedup_preserves_order() -> None:
    got = quotes_for_event(_Ev([5, 3, 5, 3]), QIDS, 30)
    assert got == ["msg_05", "msg_03"]


def test_no_indices_falls_back_to_whole_batch() -> None:
    """模型没给 source_indices → 退回整批（保住溯源能力）。"""
    assert quotes_for_event(_Ev(None), QIDS, 30) == QIDS
    assert quotes_for_event(_Ev([]), QIDS, 30) == QIDS


def test_all_out_of_range_also_falls_back() -> None:
    """越界到一个都不剩 → 退回整批，而不是挂空。"""
    assert quotes_for_event(_Ev([999]), QIDS, 30) == QIDS


def test_respects_turns_count() -> None:
    """索引上限取「本批 turns 数」与「quote_ids 长度」的较小者。"""
    got = quotes_for_event(_Ev([5]), QIDS[:3], 3)  # 只有 3 条
    assert got == QIDS[:3], "5 超出本批范围 → 退回整批"


def test_bad_index_types_ignored() -> None:
    got = quotes_for_event(_Ev(["2", None, "x", 3]), QIDS, 30)
    assert got == ["msg_02", "msg_03"]


def test_empty_quote_ids() -> None:
    assert quotes_for_event(_Ev([1, 2]), [], 0) == []
