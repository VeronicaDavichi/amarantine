"""spec 038：称呼归一与摘要护栏。

两条线：
1. **称呼句式**（`称呼X为Y` / `X（Y）`）—— 让 `王老师 = 王泳` 能变成 ALIAS_OF；
2. **摘要护栏** —— `就是` 在原话里是等价断言，在摘要里是叙述谓语，必须分开。
"""

from __future__ import annotations

import pytest

from memory.services.labeler import extract_alias_claims_verbose, label_by_rules


def _claims(text: str, source: str = "utterance") -> list[tuple[str, str, str]]:
    claims, _ = extract_alias_claims_verbose(text, source=source)
    return [(c.alias, c.canonical, c.label) for c in claims]


def _rejects(text: str, source: str = "utterance") -> list[tuple[str, str, str]]:
    _, rejects = extract_alias_claims_verbose(text, source=source)
    return [(r.alias, r.canonical, r.reason) for r in rejects]


# ------------------------------------------------------------ A1/A2 称呼句式


def test_parse_chenghex() -> None:
    """`称呼王泳为王老师` → alias=王老师, canonical=王泳, exact。"""
    assert _claims("称呼王泳为王老师") == [("王老师", "王泳", "exact")]


def test_no_duplicate_overlapping_match() -> None:
    """`称呼` 不能被拆成 `呼王泳为` 再匹配一次（重叠 span 只留一条）。"""
    claims = _claims("称呼王泳为王老师")
    assert len(claims) == 1
    assert all("呼" not in a for a, _, _ in claims)


def test_parenthesis_form() -> None:
    """`王泳（王老师）` 是最常见的备注写法。"""
    assert _claims("王泳（王老师）今天请假") == [("王老师", "王泳", "exact")]


def test_pronoun_is_not_a_name() -> None:
    """`叫他王老师` 里「他」不是人名 —— 宁可漏，也不建错边。"""
    assert _claims("大家都叫他王老师") == []


def test_non_appellation_rejected() -> None:
    """`叫他不要动`：右侧不是称呼，不产 claim。"""
    assert _claims("叫他不要动") == []


# ------------------------------------------------------------ A3 摘要护栏


def test_summary_prose_rejected() -> None:
    """`任泽安说明就是这个接口` 在摘要语境下必须拒。"""
    rejects = _rejects("任泽安说明就是这个接口", source="summary")
    assert any(r[2] == "summary_prose" for r in rejects), rejects
    assert _claims("任泽安说明就是这个接口", source="summary") == []


def test_utterance_keeps_old_behaviour() -> None:
    """原话语境保持旧行为（`就是` 仍是等价断言）。"""
    assert _claims("糖包就是豆包", source="utterance") == [("糖包", "豆包", "exact")]


def test_summary_keeps_real_declaration() -> None:
    """摘要里也可能是真声明（`陈柏林也叫陈总`），不能一刀切禁掉。"""
    assert _claims("陈柏林也叫陈总", source="summary") == [("陈柏林", "陈总", "exact")]


def test_label_by_rules_defaults_to_summary() -> None:
    """打标入口跑在摘要上，默认就是 summary 语境。"""
    result = label_by_rules("任泽安说明就是这个接口")
    assert result.alias_claims == []
    assert any(r.reason == "summary_prose" for r in result.alias_rejects)
