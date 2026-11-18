"""spec 047 延伸：emoji 昵称 → 候选别名（用户 2026-11-18 提的思路）。"""

from __future__ import annotations

from memory.services.emoji_alias import derive_names, has_emoji, load_emoji_words

WORDS = {"🦆": "鸭", "🍗": "腿", "🐟": "鱼", "🥔": "土豆", "🌙": "月"}


def test_has_emoji() -> None:
    assert has_emoji("便携式🦆🍗")
    assert not has_emoji("便携式")


def test_strip_emoji_candidate() -> None:
    """去 emoji 是最有效的一条：`便携式🦆🍗` 在语料里被叫 `便携式`（139 次）。"""
    got = derive_names("便携式🦆🍗", words=WORDS)
    assert ("便携式", "strip") in got


def test_word_substitution_candidate() -> None:
    """用户说的猜法：`🦆🍗 → 鸭腿`（组合拼接）。"""
    got = derive_names("🦆🍗", words=WORDS)
    assert ("鸭腿", "words") in got


def test_both_candidates_ordered() -> None:
    """先保守（去 emoji）后激进（替词）。"""
    got = derive_names("便携式🦆🍗", words=WORDS)
    assert [how for _c, how in got] == ["strip", "words"]


def test_numeric_only_rejected() -> None:
    """`🍚09 → 09` 这类必须拒：数字在语料里到处都有，不能当称呼。"""
    got = derive_names("🍚09", words={"🍚": "饭"})
    assert all(not c.isdigit() for c, _how in got), got


def test_single_char_rejected() -> None:
    got = derive_names("🐟", words=WORDS)
    assert all(len(c) >= 2 for c, _how in got), got


def test_no_emoji_returns_empty() -> None:
    assert derive_names("便携式", words=WORDS) == []


def test_unknown_emoji_ignored() -> None:
    """词表里没有的 emoji：只给「去 emoji」候选，不瞎猜。"""
    got = derive_names("abc🫠def", words=WORDS)
    assert all(how == "strip" for _c, how in got), got


def test_load_emoji_words_skips_meta_keys() -> None:
    words = load_emoji_words()
    assert all(not k.startswith("_") for k in words)
    assert words.get("🦆") == "鸭"
