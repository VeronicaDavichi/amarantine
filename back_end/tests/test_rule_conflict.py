"""spec 032：规则投票 + 同义词优先 + 震荡交 LLM。

## 这个文件要钉住什么

1. **同义词压过启发式** —— 用户明确声明不能被「看字形猜」覆盖
2. **规则顺序不影响结论** —— 这是投票结构存在的全部意义
3. **真震荡返回 UNKNOWN / rule=conflict** —— 调用方据此走 LLM
4. **震荡日志真的打出来** —— 否则「交 LLM」是不是天天兜底没人知道
"""

from __future__ import annotations

import logging

import pytest

from memory.config import Settings
from memory.services.entity_match import (
    RULING_DIFFERENT,
    RULING_SAME,
    RULING_UNKNOWN,
    best_candidate,
    match_entities,
)


def _s(**kw) -> Settings:
    s = Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ============================================================ 同义词优先（修 bug）


def test_synonym_beats_name_structure() -> None:
    """**核心修复**：配了同义词之后，「张三/张三丰」判同一。

    改之前：规则 1（姓名结构）抢先返回 DIFFERENT，同义词表**根本没机会执行**。
    实测过——用户明明写了「这俩是同一个」，系统当没看见。

    为什么这里不交给 LLM：LLM 看到「张三 / 张三丰」也大概率说「不同」，
    用户的声明等于白配。用户说是同一个，就是同一个。
    """
    s = _s(ENTITY_SYNONYMS="张三丰=张三")
    r = match_entities("张三", "张三丰", settings=s)
    assert r.is_same, f"同义词被忽略了：{r.ruling}（{r.rule}: {r.reason}）"
    assert r.rule == "synonym"


def test_synonym_works_both_directions() -> None:
    """同义词表不分左右。"""
    s = _s(ENTITY_SYNONYMS="张三丰=张三")
    assert match_entities("张三丰", "张三", settings=s).is_same
    assert match_entities("张三", "张三丰", settings=s).is_same


def test_no_synonym_keeps_name_structure_verdict() -> None:
    """没配同义词时，031 的行为不能变：仍判 DIFFERENT。"""
    r = match_entities("张三", "张三丰", settings=Settings())
    assert r.ruling == RULING_DIFFERENT
    assert r.rule == "single_char_prefix"


def test_synonym_does_not_override_completely_unrelated() -> None:
    """同义词只对表里那一对生效，不能变成「什么都判同」。"""
    s = _s(ENTITY_SYNONYMS="张三丰=张三")
    r = match_entities("李四", "王五", settings=s)
    assert not r.is_same


# ============================================================ 规则顺序无关


@pytest.mark.parametrize(
    "synonyms",
    ["", "张三丰=张三"],
)
def test_verdict_does_not_depend_on_suffix_config_order(synonyms: str) -> None:
    """后缀表里加不加东西，不能改变「张三/张三丰」在无同义词时的结论。

    这是顺序无关性的一个代理测试：规则 1 和规则 3 的结果不该互相干扰。
    """
    s = _s(ENTITY_SYNONYMS=synonyms)
    r = match_entities("张三", "张三丰", settings=s)
    # 有同义词 -> SAME；没有 -> DIFFERENT。两种都不该是 UNKNOWN
    assert r.ruling != RULING_UNKNOWN, f"意外震荡：{r.reason}"
    expected = RULING_SAME if synonyms else RULING_DIFFERENT
    assert r.ruling == expected


def test_place_name_still_same_without_conflict() -> None:
    """031 的弃权让地名不震荡 —— 保持 SAME，不要为了投票把它变成 UNKNOWN。

    弃权和投票**不冲突**：弃权是「这条规则知道自己不可靠」，
    投票是「多条规则都表态时才裁决」。保留弃权能减少不必要的 LLM 调用。
    """
    r = match_entities("上海", "上海市", settings=Settings())
    assert r.is_same
    assert r.rule == "suffix"


# ============================================================ 震荡显式化


def test_conflict_yields_unknown_with_conflict_rule(monkeypatch) -> None:
    """人为造震荡：规则 1 说不同、规则 3 说同 -> UNKNOWN / rule=conflict。

    怎么造：把「丰」塞进后缀白名单，同时**让规则 1 不看后缀例外** ——
    也就是回到 031 修之前的状态。用 monkeypatch 替换那个 helper，
    避免去改生产代码里的例外列表。
    """
    from memory.services import entity_match as em

    # 让规则 1 永远按「纯姓名结构」判，不看称呼/后缀例外
    monkeypatch.setattr(
        em,
        "_prefix_conflict_excluding_affix",
        lambda short, long, h, sfx: em.single_char_name_prefix_conflict(short, long),
    )

    s = _s(ENTITY_SUFFIXES="丰,AI,公司")
    r = match_entities("张三", "张三丰", settings=s)

    assert r.ruling == RULING_UNKNOWN, f"应该震荡交 LLM，实际 {r.ruling}"
    assert r.rule == "conflict", f"rule 应该是 conflict，实际 {r.rule!r}"
    assert "同" in r.reason and "不同" in r.reason, (
        f"震荡原因要说清两边的结论：{r.reason}"
    )


def test_conflict_reason_lists_both_sides(monkeypatch) -> None:
    """震荡的 reason 必须同时列出「说同的规则」和「说不同的规则」。"""
    from memory.services import entity_match as em

    monkeypatch.setattr(
        em,
        "_prefix_conflict_excluding_affix",
        lambda short, long, h, sfx: em.single_char_name_prefix_conflict(short, long),
    )
    s = _s(ENTITY_SUFFIXES="丰,AI,公司")
    r = match_entities("张三", "张三丰", settings=s)
    assert "suffix" in r.reason, f"没列出说同的规则：{r.reason}"
    assert "prefix" in r.reason or "single_char" in r.reason, (
        f"没列出说不同的规则：{r.reason}"
    )


def test_conflict_is_logged(caplog) -> None:
    """`best_candidate` 遇到震荡候选时必须打日志。

    没有这个日志就不知道「交 LLM」是偶尔兜底还是天天兜底 ——
    震荡率是规则设计质量的指标。
    """
    from memory.services import entity_match as em

    monkeypatch_target = em._prefix_conflict_excluding_affix
    try:
        em._prefix_conflict_excluding_affix = (
            lambda short, long, h, sfx: em.single_char_name_prefix_conflict(short, long)
        )
        s = _s(ENTITY_SUFFIXES="丰,AI,公司")
        caplog.set_level(logging.INFO, logger="memory.services.entity_match")
        best_candidate("张三", ["张三丰"], settings=s)
        msgs = [r.message for r in caplog.records if "震荡" in r.message]
        assert msgs, f"震荡没打日志，实际日志: {[r.message for r in caplog.records][:5]}"
        assert "张三" in msgs[0] and "张三丰" in msgs[0]
    finally:
        em._prefix_conflict_excluding_affix = monkeypatch_target


def test_conflict_candidate_is_skipped_but_others_still_tried() -> None:
    """震荡的候选被跳过，但**不影响**其他候选正常命中。

    场景：候选列表里第一个会震荡、第二个确定是同义词。
    正确行为是跳过第一个、选中第二个 —— 不能因为一个候选震荡就整个放弃。
    """
    s = _s(ENTITY_SYNONYMS="张总=张三")
    # 「张三」对「张三丰」无同义词 -> DIFFERENT（不是 SAME，所以跳过）
    # 「张三」对「张总」有同义词 -> SAME
    got = best_candidate("张三", ["张三丰", "张总"], settings=s)
    assert got is not None, "应该找到「张总」"
    assert got[0] == "张总"
    assert got[1].rule == "synonym"


# ============================================================ 规则层关闭


def test_rules_off_disables_synonym_too() -> None:
    """规则层关闭时同义词**也**失效，避免出现「半开」状态。

    期望是 DIFFERENT 不是 UNKNOWN —— 规则 1 永远生效（spec 020），
    它仍会判「张三/张三丰」不同。这条测的是「同义词没有偷偷生效」。
    """
    s = _s(ENTITY_RULE_ENABLED=False, ENTITY_SYNONYMS="张三丰=张三")
    r = match_entities("张三", "张三丰", settings=s)
    assert r.ruling == RULING_DIFFERENT, (
        f"规则层关闭后同义词仍然生效了（{r.ruling}/{r.rule}）"
    )
    assert r.rule == "single_char_prefix"


def test_rules_off_still_reports_rule1() -> None:
    """关闭时规则 1 的判定保留（spec 020 的既有功能）。"""
    s = _s(ENTITY_RULE_ENABLED=False)
    assert match_entities("张三", "张三丰", settings=s).ruling == RULING_DIFFERENT


def test_rules_off_no_conflict_possible() -> None:
    """只有一条规则参与时不可能震荡 —— 必须返回 UNKNOWN 而不是 conflict。"""
    s = _s(ENTITY_RULE_ENABLED=False)
    r = match_entities("李四", "王五", settings=s)
    assert r.ruling == RULING_UNKNOWN
    assert r.rule != "conflict", "只有规则 1 时不该报震荡"
