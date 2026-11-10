"""补 040 的长卡切分测试（embedding 8192 token 上限）。"""

from __future__ import annotations

from memory.services.raw_quote import MAX_CARD_CHARS, _split_long_parts


def test_short_parts_untouched() -> None:
    assert _split_long_parts(["短句一", "短句二"]) == ["短句一", "短句二"]


def test_long_part_split_by_lines() -> None:
    """超长「整段卡」按行再切，避免 embedding 413（实测 10334 token 被拒）。"""
    long_text = "\n".join(f"user: 第 {i} 条消息" for i in range(400))
    assert len(long_text) > MAX_CARD_CHARS
    parts = _split_long_parts([long_text])
    assert len(parts) > 1
    assert all(len(p) <= MAX_CARD_CHARS for p in parts)
    # 不丢内容：拼回去的行数一致
    assert sum(p.count("\n") + 1 for p in parts) == 400


def test_single_huge_line_hard_split() -> None:
    """单行超长时硬切。"""
    parts = _split_long_parts(["x" * (MAX_CARD_CHARS * 2 + 10)])
    assert len(parts) == 3
    assert all(len(p) <= MAX_CARD_CHARS for p in parts)


def test_empty_input_kept_safe() -> None:
    assert _split_long_parts([]) == []
    assert _split_long_parts(["   "]) == []
