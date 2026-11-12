"""spec 047：群头衔系统公告解析（真实文案驱动）。"""

from __future__ import annotations

from memory.services import group_title as G


def test_parse_real_notices() -> None:
    """实测群 485490399 的真实公告。"""
    cases = {
        "恭喜我是给AI大人散热的工程师，智械了别杀我获得群主授予的机仆头衔":
            ("我是给AI大人散热的工程师，智械了别杀我", "机仆"),
        "恭喜羽生玲央获得群主授予的羽生头衔": ("羽生玲央", "羽生"),
        "恭喜棉棉获得群主授予的妈妈桑头衔": ("棉棉", "妈妈桑"),
        "恭喜棉棉获得群主授予的喵喵喵头衔": ("棉棉", "喵喵喵"),
        "恭喜素笺获得群主授予的莫比乌斯车的头衔": ("素笺", "莫比乌斯车"),
    }
    for text, expected in cases.items():
        assert G.parse_title_notice(text) == expected, text


def test_non_title_text_returns_none() -> None:
    for text in ("今天吃了火锅", "恭喜你中奖了", "头衔是什么"):
        assert G.parse_title_notice(text) is None, text


def test_empty_text() -> None:
    assert G.parse_title_notice("") is None
    assert G.parse_title_notice(None) is None  # type: ignore[arg-type]


def test_collect_dedupes_duplicates() -> None:
    """语料里同一条公告会重复出现（工具返回重复行）。"""
    texts = [
        "恭喜棉棉获得群主授予的妈妈桑头衔",
        "恭喜棉棉获得群主授予的妈妈桑头衔",
        "恭喜羽生玲央获得群主授予的羽生头衔",
    ]
    assert G.collect_title_notices(texts) == [
        ("棉棉", "妈妈桑"),
        ("羽生玲央", "羽生"),
    ]
