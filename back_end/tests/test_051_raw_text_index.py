# -*- coding: utf-8 -*-
"""T1/T2 单测：两道闸、token 拼接、材料贪心填充。"""

from __future__ import annotations

import pytest

from memory.services import raw_text_index as R

#: 实测样本（真实语料里取出来的，不是编的）
MARKETING = (
    "运营：【尾款链接整合①】Fate strange Fake FSF奇异赝品再贩 76徽章 "
    "https://item.taobao.com/item.htm?id=123456 定金已开 截单 7.20 "
    "[图片] 6A6157ACEF0EFBE980D34CE3511DE24A.png"
)
QQ_CARD = ('不在线，有问题请联系淘宝店铺旺旺客服：{"app":"com.tencent.mannounce",'
           '"bizsrc":"","config":{"ctime":1785553017}}')
NORMAL = "上厕所不冲厕所的吗！！！！"
NORMAL2 = "七点钟开始洗的现在还在转"


def test_should_index_keeps_normal() -> None:
    assert R.should_index(NORMAL) is True
    assert R.should_index(NORMAL2) is True


def test_should_index_drops_marketing() -> None:
    assert R.should_index(MARKETING) is False


def test_should_index_drops_qq_card_json() -> None:
    assert R.should_index(QQ_CARD) is False


@pytest.mark.parametrize("text", ["", "   ", "草", "哈哈", "[图片]"])
def test_should_index_drops_too_short(text: str) -> None:
    assert R.should_index(text) is False


def test_filter_keeps_order() -> None:
    got = R.filter_quotes([NORMAL, MARKETING, NORMAL2, QQ_CARD])
    assert got == [NORMAL, NORMAL2]


def test_build_tokens_empty_when_all_gated() -> None:
    assert R.build_tokens([MARKETING, QQ_CARD]) == ""


def test_build_tokens_has_no_cross_quote_bigram() -> None:
    """拼接必须有分隔：否则上一条结尾 + 下一条开头会造出假 bigram。"""
    a, b = "今天下雨了", "明天放晴吧"
    joined = R.build_tokens([a, b])
    toks = joined.split()
    # 「雨了」是 a 自己的（下雨了），合法；「了明」只可能来自跨条相接，必须没有
    assert "了明" not in toks, f"跨条假 bigram 出现了: {toks}"
    for tok in ("今天", "下雨", "明天", "放晴"):
        assert tok in toks


def test_build_tokens_truncates_by_whole_quote() -> None:
    """截断按整条砍，不砍半句。"""
    long_a = "甲" * 30
    long_b = "乙" * 30
    got = R.build_tokens([long_a, long_b], max_chars=40)
    assert "甲" in got
    assert "乙" not in got, "第二条应整条丢弃，而不是给半句"


def test_material_quotes_respects_budget() -> None:
    texts = ["甲" * 100, "乙" * 100]
    quotes, used, truncated = R.material_quotes(texts, max_chars=2000, budget_left=150)
    assert quotes == ["甲" * 100]
    assert used == 100
    assert truncated is True


def test_material_quotes_zero_budget_gives_nothing() -> None:
    """预算用尽 → 什么都不给（而不是给空数组假装「没有原文」）。"""
    quotes, used, truncated = R.material_quotes([NORMAL], max_chars=2000, budget_left=0)
    assert (quotes, used, truncated) == ([], 0, False)


def test_material_quotes_marks_untruncated_when_fits() -> None:
    quotes, used, truncated = R.material_quotes([NORMAL, NORMAL2],
                                                max_chars=2000, budget_left=2000)
    assert quotes == [NORMAL, NORMAL2]
    assert truncated is False


def test_material_quotes_max_chars_boundary() -> None:
    """2000 字上限的边界：1999 / 2000 / 2001。"""
    exact = "x" * 2000
    q1, u1, t1 = R.material_quotes([exact], max_chars=2000, budget_left=9999)
    assert (q1, u1, t1) == ([exact], 2000, False)
    over = "x" * 2001
    q2, u2, t2 = R.material_quotes([over], max_chars=2000, budget_left=9999)
    assert q2 == [] and t2 is True
    under = "x" * 1999
    q3, u3, t3 = R.material_quotes([under], max_chars=2000, budget_left=9999)
    assert (q3, u3, t3) == ([under], 1999, False)


def test_material_quotes_skips_marketing_but_reports_truncation() -> None:
    quotes, used, truncated = R.material_quotes([MARKETING, NORMAL],
                                                max_chars=2000, budget_left=2000)
    assert quotes == [NORMAL]
    assert truncated is False, "被闸挡掉不算截断 —— 那些内容本来就不该给"
    assert used == len(NORMAL)
