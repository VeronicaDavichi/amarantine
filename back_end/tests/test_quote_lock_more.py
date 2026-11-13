"""spec 041：繁中引号/短句 + 空格短词锁 token。"""

from __future__ import annotations

from memory.services import reference as R


def _words(text: str) -> list[str]:
    return [w for w, _ in R._tokenize(text)]


def test_traditional_quotes_locked() -> None:
    for text, expect in (
        ("『那些年』好听", "『那些年』"),
        ("“人是猫”这首歌", "“人是猫”"),
        ("‘好’字", "‘好’"),
        ("«bonjour» 是法语", "«bonjour»"),
        ("【本日上新】内容", "【本日上新】"),
        ("﹁好﹂", "﹁好﹂"),
    ):
        words = _words(text)
        assert expect in words, (text, words)


def test_short_quote_locked() -> None:
    """短句引号也要锁：`「好」` 不能被拆。"""
    words = _words("他说「好」然后走了")
    assert "「好」" in words, words


def test_spaced_short_word_locked() -> None:
    """前后有空格的短词（` lysk `、` dd `、` 666 `）锁成一个 token。"""
    words = _words("这个 lysk 不错 dd 666 收")
    assert "lysk" in words, words
    assert "dd" in words, words
    assert "666" in words, words


def test_space_token_does_not_break_neighbors() -> None:
    words = _words("今天 天气 不错")
    assert "今天" in words and "天气" in words and "不错" in words, words
