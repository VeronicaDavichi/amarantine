"""spec 047：人名标记分类与同名校验（真实群数据驱动）。"""

from __future__ import annotations

from memory.services import name_marker as N


# ------------------------------------------------------------ 名片分类


def test_moderation_card_is_not_a_name() -> None:
    """实测：23 个号被管理员改成 `合并转发存在广告,禁言15分钟` —— 不是人名。"""
    for text in (
        "合并转发存在广告,禁言15分钟",
        "文本存在广告,禁言15分钟",
        "管理禁止内容,禁言15天",
    ):
        v = N.classify_card(text)
        assert v.kind == N.KIND_MODERATION, text
        assert not v.usable


def test_whitespace_card_is_troll() -> None:
    """全空白/控制字符 → 整活人，不作为名字。"""
    for text in ("　", "　　　　　", "\u200b", "\t \n"):
        v = N.classify_card(text)
        assert v.kind == N.KIND_BLANK_WS, repr(text)
        assert not v.usable


def test_punctuation_card_is_blank() -> None:
    """纯标点（`……` / `.`）→ 无意义名，不作为名字。"""
    for text in ("……", ".", "……!"):
        v = N.classify_card(text)
        assert v.kind in (N.KIND_BLANK_PUNCT, N.KIND_BLANK_WS), repr(text)
        assert not v.usable


def test_alt_suffix_extracts_base() -> None:
    """`往汐（北瑀家的鱼）复活版` → base=往汐，kind=alt。"""
    v = N.classify_card("往汐（北瑀家的鱼）复活版")
    assert v.kind == N.KIND_ALT
    assert v.base == "往汐"


def test_normal_name() -> None:
    v = N.classify_card("秋")
    assert v.kind == N.KIND_NORMAL
    assert v.base == "秋"
    assert v.usable


# ------------------------------------------------------------ 分桶判定


def _m(uid, card, nick="nick"):
    return {"uid": uid, "qq": uid, "nickname": nick, "card": card}


def test_impersonation_when_many_same_name() -> None:
    """实测：21 个不同的人把名片改成「刘芮西宝宝」→ 整活，不关联。"""
    members = [_m(f"u{i}", "刘芮西宝宝") for i in range(21)]
    buckets = N.analyze_group(members)
    assert len(buckets) == 1
    assert buckets[0].kind == "impersonation"
    assert len(buckets[0].accounts) == 21


def test_same_owner_when_two_with_alt_suffix() -> None:
    """往汐 + 复活版 → 可以建 SAME_OWNER。"""
    members = [
        _m("u1", "往汐（北瑀家的鱼）"),
        _m("u2", "往汐（北瑀家的鱼）复活版"),
    ]
    buckets = N.analyze_group(members)
    assert len(buckets) == 1
    assert buckets[0].kind == "same_owner"


def test_ambiguous_when_same_name_no_suffix() -> None:
    """秋×2 / 咸鱼×2 → 同名但无小号证据，只留痕不关联。"""
    members = [_m("u1", "秋"), _m("u2", "秋")]
    buckets = N.analyze_group(members)
    assert buckets[0].kind == "ambiguous"


def test_moderation_cards_excluded_from_buckets() -> None:
    """23 个处罚名片不能进分桶（否则会变成「整活组」）。"""
    members = [_m(f"u{i}", "合并转发存在广告,禁言15分钟") for i in range(23)]
    members.append(_m("x", "秋"))
    assert N.analyze_group(members) == []


def test_threshold_boundary() -> None:
    """2 个同名人 → ambiguous；3 个 → impersonation。"""
    assert N.analyze_group([_m("a", "秋"), _m("b", "秋")])[0].kind == "ambiguous"
    three = [_m("a", "秋"), _m("b", "秋"), _m("c", "秋")]
    assert N.analyze_group(three)[0].kind == "impersonation"
