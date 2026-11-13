"""spec 041：书名号里的「X 是 Y」不算定义（评测暴露的误绑）。"""

from __future__ import annotations

from memory.services.reference import extract_signals


def _defines(text: str) -> list[tuple[str, str]]:
    return [(s.surface, s.gloss or "") for s in extract_signals(text) if s.kind == "define"]


def test_title_not_treated_as_definition() -> None:
    """`《人是猫》完整版` 曾被抽成 `人 → 猫》完整版`。"""
    got = _defines("今天听了《人是猫》完整版，真的好听")
    assert all(surface != "人" for surface, _ in got), got


def test_real_definition_still_works() -> None:
    got = _defines("鱼就是那个 deepseek 的 bot")
    assert any(surface == "鱼" and "deepseek" in gloss for surface, gloss in got), got


def test_fragment_gloss_rejected() -> None:
    """gloss 以标点开头/结尾的碎片不要。"""
    for text in ("这个就是，随便说说", "前提是；口语过关"):
        for surface, gloss in _defines(text):
            assert gloss[0] not in "，。；、！？"
