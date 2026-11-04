"""A16：chunk 切分与「每卡保留 N 个」的纯函数测试。

`core/chunker.py` 不碰 IO，所以这里全部是真单测 —— 不需要 fake 任何东西。
"""

from __future__ import annotations

import pytest

from memory.core.chunker import Chunk, keep_top_n, split_text


# ---------------- D5：确定性 ----------------


def test_same_input_same_output() -> None:
    text = "张三决定项目A延期到六月。" * 20
    a = split_text(text, size=64, overlap=8)
    b = split_text(text, size=64, overlap=8)
    assert a == b, "切分不确定 —— 同输入必须同输出"


def test_ids_and_text_are_stable_across_runs() -> None:
    text = "x" * 300
    first = [(c.index, c.start, c.end) for c in split_text(text, size=100, overlap=10)]
    second = [(c.index, c.start, c.end) for c in split_text(text, size=100, overlap=10)]
    assert first == second


# ---------------- 基本形状 ----------------


def test_short_text_is_single_chunk() -> None:
    chunks = split_text("项目A延期", size=100, overlap=10)
    assert len(chunks) == 1
    assert chunks[0] == Chunk(index=0, text="项目A延期", start=0, end=5)


def test_empty_text_returns_no_chunks() -> None:
    """空文本返回 `[]`，不是 `[""]` —— 空块没有检索意义。"""
    assert split_text("", size=100, overlap=10) == []
    assert split_text("   \n\t ", size=100, overlap=10) == []


def test_long_text_is_split_with_overlap() -> None:
    text = "a" * 300
    chunks = split_text(text, size=100, overlap=20)
    assert len(chunks) > 1
    # 相邻块必须有重叠，且重叠长度 = overlap
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start == prev.start + (100 - 20)
        assert prev.end - nxt.start == 20


def test_chunks_cover_the_whole_text() -> None:
    """所有块拼起来（含重叠）必须覆盖原文，不能漏字。"""
    text = "".join(str(i % 10) for i in range(500))
    chunks = split_text(text, size=80, overlap=16)
    covered = set()
    for c in chunks:
        covered.update(range(c.start, c.end))
    assert covered == set(range(len(text))), "有字符没被任何块覆盖"


def test_last_chunk_may_be_short() -> None:
    text = "b" * 250
    chunks = split_text(text, size=100, overlap=10)
    assert chunks[-1].end == len(text)
    assert len(chunks[-1].text) <= 100


def test_trailing_whitespace_does_not_produce_empty_chunk() -> None:
    text = "c" * 100 + " " * 100
    chunks = split_text(text, size=100, overlap=10)
    assert all(c.text.strip() for c in chunks), "生成了纯空白块"


# ---------------- 参数校验 ----------------


@pytest.mark.parametrize(
    ("size", "overlap"),
    [(0, 0), (-1, 0), (100, -1), (100, 100), (100, 200)],
)
def test_invalid_params_raise(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        split_text("abc" * 100, size=size, overlap=overlap)


# ---------------- D1/D2：每卡保留 N 个 ----------------


def test_keep_top_n_limits_per_parent() -> None:
    """D1：一张卡 5 个 chunk，N=2 → 只留 2 个。"""
    scored = [("evt_a", 0.9), ("evt_a", 0.8), ("evt_a", 0.7), ("evt_a", 0.6), ("evt_a", 0.5)]
    kept = keep_top_n(scored, n=2)
    assert len(kept) == 2
    assert all(p == "evt_a" for p, _ in kept)


def test_keep_top_n_takes_highest_scores() -> None:
    """D2：保留的是分数最高的那两个（顺序可验证）。"""
    scored = [("evt_a", 0.1), ("evt_a", 0.99), ("evt_a", 0.5), ("evt_a", 0.88)]
    kept = keep_top_n(scored, n=2)
    assert [s for _, s in kept] == [0.99, 0.88]


def test_keep_top_n_is_per_parent_not_global() -> None:
    """两个父事件各 2 个，N=2 → 共保留 4 项，不是 2 项。"""
    scored = [
        ("evt_a", 0.9),
        ("evt_b", 0.85),
        ("evt_a", 0.8),
        ("evt_b", 0.7),
        ("evt_a", 0.1),
        ("evt_b", 0.2),
    ]
    kept = keep_top_n(scored, n=2)
    assert len(kept) == 4
    assert sum(1 for p, _ in kept if p == "evt_a") == 2
    assert sum(1 for p, _ in kept if p == "evt_b") == 2


def test_keep_top_n_zero_returns_nothing() -> None:
    """`n=0` 表示「不保留」，不是「不限」—— 调用方必须显式判开关。"""
    assert keep_top_n([("evt_a", 0.9)], n=0) == []
    assert keep_top_n([("evt_a", 0.9)], n=-1) == []


def test_keep_top_n_preserves_order_on_ties() -> None:
    """同分时按出现顺序，保证确定性（不能靠 dict 顺序）。

    注意 `n=2` 是**每个父**的上限，不是总条数 ——
    这里 evt_a 恰好 2 个、evt_b 1 个，都没超，所以 3 项全留。
    """
    scored = [("evt_a", 0.5), ("evt_b", 0.5), ("evt_a", 0.5)]
    kept = keep_top_n(scored, n=2)
    assert [p for p, _ in kept] == ["evt_a", "evt_b", "evt_a"]
    assert len(kept) == 3
