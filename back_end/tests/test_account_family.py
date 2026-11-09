"""spec 042：账号族（词形族 blocking / 归属断言 / 只加边不并节点）。

覆盖 A1（同名不同 uid 出 SAME_AS 候选）、A2（question 不建边）、
A3（空槽不建边）、A6（重复断言防刷由 041 的 resolve_status 保证）。
"""

from __future__ import annotations

from memory.services import account_family as AF
from memory.services import reference as R


def _acc(uid: str, nick: str) -> AF.AccountRecord:
    return AF.AccountRecord(uid=uid, nick=nick, namespace="ns")


# ------------------------------------------------------------ A1 blocking


def test_same_nick_different_uid_is_probable() -> None:
    """A1：3 个「智小安业务助手」→ 一条 same_nick 候选，**不合并**。"""
    accounts = [
        _acc("111", "智小安业务助手"),
        _acc("222", "智小安业务助手"),
        _acc("333", "智小安业务助手"),
    ]
    fams = AF.find_families(accounts)
    same = [f for f in fams if f.kind == "same_nick"]
    assert len(same) == 1
    assert same[0].members == ("111", "222", "333")
    assert same[0].label == "probable"


def test_different_nicks_no_same_candidate() -> None:
    accounts = [_acc("111", "王泳"), _acc("222", "刘阅")]
    assert [f for f in AF.find_families(accounts) if f.kind == "same_nick"] == []


def test_suffix_family_candidate() -> None:
    """`xxxx的大肥鱼` / `xxx的鱼` → 同族（uncertain）。"""
    accounts = [_acc("111", "xxxx的大肥鱼"), _acc("222", "yyy的鱼")]
    fams = AF.find_families(accounts)
    suffix = [f for f in fams if f.kind == "suffix"]
    assert suffix and suffix[0].family == "鱼"
    assert suffix[0].label == "uncertain"


def test_prefix_is_weak_candidate() -> None:
    """前缀相同 → 弱候选（uncertain），不是同人证据。"""
    accounts = [_acc("111", "智小安业务助手"), _acc("222", "智小安助理")]
    fams = AF.find_families(accounts)
    prefix = [f for f in fams if f.kind == "prefix"]
    assert prefix and prefix[0].label == "uncertain"


def test_family_of_plain_nick_is_none() -> None:
    assert AF.family_of("王泳") is None
    assert AF.family_of("xxxx的大肥鱼") == "鱼"


# ------------------------------------------------------------ A2/A3 断言


def test_question_claim_not_buildable() -> None:
    """A2：`X这个账号是鱼吧？` 只记 question，不建边。"""
    claims = AF.extract_account_claims("492988054这个账号是鱼吧？")
    assert claims and claims[0].mode == "question"
    assert not AF.claim_can_build_edge(claims[0])


def test_empty_slot_not_buildable() -> None:
    """A3：`X账号是谁的鱼` → 空槽，不建边。"""
    claims = AF.extract_account_claims("492988054这个账号是谁的鱼")
    assert claims
    assert not AF.claim_can_build_edge(claims[0])


def test_deny_claim_recognized() -> None:
    claims = AF.extract_account_claims("不是那个鱼")
    assert claims and claims[0].mode == "deny"


# ------------------------------------------------------------ 复用 041 的裁决


def test_convergence_reuses_reference_rules() -> None:
    """A4/A6：两条 assert 才 probable；同一人重复说算 1 条（由 resolve_status 保证）。"""
    one = R.Claim("鱼", "session:x", "bot", "assert")
    assert R.resolve_status("uncertain", [one]) == "uncertain"
    assert R.resolve_status("uncertain", [one, one]) == "probable"


def test_confirm_and_deny_semantics() -> None:
    """A5：deny 后 strict 不再命中（复用 tier 规则）。"""
    assert not R.tier_allows("denied", "strict")
    assert R.tier_allows("denied", "explore")


# ------------------------------------------------------------ T5/T6 三档查询


def test_labels_for_tier_maps_to_statuses() -> None:
    """strict 只认 exact；default 含 probable；explore 全含。"""
    assert AF.labels_for_tier("strict") == ["exact"]
    assert "probable" in AF.labels_for_tier("default")
    assert "uncertain" in AF.labels_for_tier("explore")


def test_labels_for_unknown_tier_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        AF.labels_for_tier("nope")


def test_family_query_cypher_covers_both_edges() -> None:
    """查询要同时覆盖 SAME_AS 与 INSTANCE_OF，否则复核队列看不全。"""
    assert "SAME_AS" in AF.QUERY_FAMILIES_CYPHER
    assert "INSTANCE_OF" in AF.QUERY_FAMILIES_CYPHER
    assert "$labels" in AF.QUERY_FAMILIES_CYPHER


def test_family_verdict_cypher_targets_both_kinds() -> None:
    """确认/否认要能区分两类边：SAME_AS 比 uid，INSTANCE_OF 比主体名。"""
    assert "b.uid = $target" in AF.SET_FAMILY_LABEL_CYPHER
    assert "b.name = $target" in AF.SET_FAMILY_LABEL_CYPHER
