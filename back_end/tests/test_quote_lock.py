"""spec 041：书名号/引号整段锁成一个 token（分词层根因）。"""

from __future__ import annotations

from memory.services import reference as R


def test_title_stays_one_token() -> None:
    """`《人是猫》` 不能被切成 人/是/猫。"""
    tokens = R._tokenize("今天听了《人是猫》完整版")
    words = [w for w, _ in tokens]
    assert "《人是猫》" in words, words
    assert "人" not in words, words


def test_quote_variant_locked() -> None:
    tokens = R._tokenize("「那些年」很好听")
    words = [w for w, _ in tokens]
    assert "「那些年」" in words, words


def test_no_definition_from_title() -> None:
    sigs = R.extract_signals("今天听了《人是猫》完整版，真的好听")
    defines = [(s.surface, s.gloss) for s in sigs if s.kind == "define"]
    assert all(surface != "人" for surface, _ in defines), defines


def test_normal_definition_unaffected() -> None:
    sigs = R.extract_signals("鱼就是那个 deepseek 的 bot")
    assert any(s.kind == "define" and s.surface == "鱼" for s in sigs)
