"""spec 041 M2：Claim 三态与状态裁决（A3 / A7）。

覆盖：
- `assert` 累计到 2 条升 probable
- `question`（`X是Y吧？`）不建边、不计入阈值
- `deny` → denied；人确认的 `exact` 不被断言降级
- 两个不同 gloss 都被断言 → conflict
"""

from __future__ import annotations

from memory.services import reference as R


def _claim(mode: str, gloss: str, term: str = "鱼") -> R.Claim:
    return R.Claim(term=term, scope="session:x", gloss=gloss, mode=mode)


# ------------------------------------------------------------ 抽取


def test_assert_extracted() -> None:
    claims = R.extract_claims("鱼就是 deepseek 的 bot", scope="session:x")
    assert claims and claims[0].mode == "assert"
    assert claims[0].term == "鱼"


def test_question_marked_not_assert() -> None:
    """A3：`X是Y吧？` 只记 question。"""
    claims = R.extract_claims("鱼是 deepseek 的 bot 吧？", scope="session:x")
    assert claims and claims[0].mode == "question"


def test_deny_extracted() -> None:
    claims = R.extract_claims("鱼不是那个 bot", scope="session:x")
    assert claims and claims[0].mode == "deny"


def test_empty_or_plain_text_no_claims() -> None:
    for text in ("", "明天开会", "接口字段对不上"):
        assert R.extract_claims(text, scope="session:x") == []


# ------------------------------------------------------------ 裁决


def test_single_assert_keeps_uncertain() -> None:
    """单条 assert 不足以升 probable。"""
    assert R.resolve_status("uncertain", [_claim("assert", "bot")]) == "uncertain"


def test_two_asserts_become_probable() -> None:
    status = R.resolve_status("uncertain", [_claim("assert", "bot"), _claim("assert", "bot")])
    assert status == "probable"


def test_question_does_not_count() -> None:
    """A3：question 不参与升级阈值。"""
    claims = [_claim("assert", "bot"), _claim("question", "bot")]
    assert R.resolve_status("uncertain", claims) == "uncertain"


def test_deny_wins() -> None:
    """A7：否认压过一切。"""
    claims = [_claim("assert", "bot"), _claim("assert", "bot"), _claim("deny", "bot")]
    assert R.resolve_status("probable", claims) == "denied"


def test_human_exact_not_downgraded_by_asserts() -> None:
    """人确认过的 exact 不因断言被降级。"""
    assert R.resolve_status("exact", [_claim("assert", "bot")]) == "exact"


def test_human_exact_can_be_denied() -> None:
    """但否认仍可推翻 exact。"""
    assert R.resolve_status("exact", [_claim("deny", "bot")]) == "denied"


def test_two_glosses_conflict() -> None:
    """两个不同 gloss 都被断言 → conflict。"""
    claims = [_claim("assert", "bot"), _claim("assert", "动物")]
    assert R.resolve_status("uncertain", claims) == "conflict"


def test_conflict_status_not_in_default_tier() -> None:
    """conflict 不能进 default 查询（只有 explore 可见）。"""
    assert not R.tier_allows("conflict", "default")
    assert R.tier_allows("conflict", "explore")


def test_question_does_not_create_supports_edge() -> None:
    """REQ-4：`question` 只记证据，**不建 SUPPORTS 边**。

    实测踩过：第一版 cypher 对所有 claim 都 MERGE 了 SUPPORTS，
    结果 5 条 `X是Y吧？` 也贡献了支持边。
    """
    assert "item.mode <> 'question'" in R.APPLY_CLAIM_CYPHER


def test_question_not_counted_in_status() -> None:
    """A3：question 不计入升级阈值（2 条 question 也不升级）。"""
    claims = [R.Claim("鱼", "s", "bot", "question"), R.Claim("鱼", "s", "bot", "question")]
    assert R.resolve_status("uncertain", claims) == "uncertain"
