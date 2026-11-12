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
    for text in ("　", "　　　　　", "\u200b\u200b", "\t \n"):
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


# ------------------------------------------------------------ 伪装名（双向控制符）


def test_bidi_control_char_is_spoofed() -> None:
    """`AB\u202ECD` 渲染像 `ABCD`，但不是同名 —— 是伪装。"""
    clean = "ABCD"
    spoof = "AB\u202ECD"
    assert N.classify_card(clean).kind == N.KIND_NORMAL
    assert N.classify_card(spoof).kind == N.KIND_SPOOFED
    assert not N.classify_card(spoof).usable


def test_spoofed_never_merges_with_clean_name() -> None:
    """伪装名和干净名即使 base 看起来一样，也**不能**进同一个桶。"""
    members = [
        {"uid": "u1", "qq": 1, "nickname": "x", "card": "往汐"},
        {"uid": "u2", "qq": 2, "nickname": "y", "card": "往汐\u202E"},
    ]
    assert N.analyze_group(members) == []


def test_zero_width_name_is_spoofed() -> None:
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        v = N.classify_card(f"往汐{ch}")
        assert v.kind == N.KIND_SPOOFED, repr(ch)


def test_similar_names_are_not_linked() -> None:
    """很像但不是字节相同（如多一个点）→ 不合并，各自 normal。"""
    members = [
        {"uid": "u1", "qq": 1, "nickname": "x", "card": "往汐"},
        {"uid": "u2", "qq": 2, "nickname": "y", "card": "往汐。"},
    ]
    buckets = N.analyze_group(members)
    assert all(b.kind == "ambiguous" for b in buckets) or buckets == []


def test_spoofed_names_never_link_to_each_other() -> None:
    """两个伪装名只差一两个字，也**不能**互相关联（多是复制别人的模板）。"""
    members = [
        {"uid": "u1", "qq": 1, "nickname": "a", "card": "往汐\u202E"},
        {"uid": "u2", "qq": 2, "nickname": "b", "card": "往汐\u202E酱"},
    ]
    assert N.analyze_group(members) == []


def test_identical_spoofed_names_excluded_too() -> None:
    """即使两个伪装名字节完全相同，也不进桶（模板复制）。"""
    card = "往汐\u202E"
    members = [{"uid": f"u{i}", "qq": i, "nickname": "x", "card": card} for i in range(5)]
    assert N.analyze_group(members) == []


def test_excluded_collected_for_audit() -> None:
    """被排除的名片要能被审计看到，不能静默丢。"""
    members = [
        {"uid": "u1", "qq": 1, "nickname": "x", "card": "合并转发存在广告,禁言15分钟"},
        {"uid": "u2", "qq": 2, "nickname": "y", "card": "　"},
        {"uid": "u3", "qq": 3, "nickname": "z", "card": "往汐\u202E"},
        {"uid": "u4", "qq": 4, "nickname": "w", "card": "秋"},
    ]
    excluded = N.collect_excluded(members)
    kinds = {e["kind"] for e in excluded}
    assert kinds == {"moderation", "blank_ws", "spoofed"}
    assert len(excluded) == 3


def test_near_identical_normal_names_not_merged() -> None:
    """不做编辑距离合并：`往汐` 与 `往汐酱` 是两个独立名字。"""
    members = [
        {"uid": "u1", "qq": 1, "nickname": "x", "card": "往汐"},
        {"uid": "u2", "qq": 2, "nickname": "y", "card": "往汐酱"},
    ]
    assert N.analyze_group(members) == []
