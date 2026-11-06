"""spec 035：用权威名册提取人名。

## 背景

`extract_entities("陈柏林")` 曾被 jieba 切成 `[('陈','zg'), ('柏林','nr')]`
—— 「柏林」是德国首都（词典里有），姓氏被丢掉，实体名变成「柏林」。

影响面实测 14 个人名里 7 个坏的，其中「李长春」「王光明」这类会**整个人消失**
（返回 `[]`），比「名字不对」严重得多。

名册来自数据源本身（DingTalk contacts / 群标题 / sender_name），
从这些出来的字符串**就是人名**，不需要用通用词典去猜。
"""

from __future__ import annotations

import pytest

from memory.services.labeler import (
    extract_entities,
    register_roster,
    reset_roster,
    roster_names,
)


@pytest.fixture(autouse=True)
def _clean_roster():
    """每个测试前后都清空名册，避免测试间互相污染。"""
    reset_roster()
    yield
    reset_roster()


# ============================================================ 核心：名册里的人名


@pytest.mark.parametrize(
    "name",
    [
        "陈柏林",  # 姓 + 首都名
        "李长春",  # 姓 + 城市名
        "刘北京",
        "陈上海",
        "李向前",  # 姓 + 普通词
        "王光明",
        "赵和平",
    ],
)
def test_roster_name_is_extracted_intact(name: str) -> None:
    """名册里的名字必须被**完整**提取。

    这 7 个都是实测被 jieba 切坏的 —— 前 5 个症状是「吞姓氏」或「整体消失」，
    共同点是「姓 + jieba 认识的词」。
    """
    register_roster([name])
    ents = extract_entities(f"{name}说了一句话")
    names = [e.name for e in ents]
    assert name in names, (
        f"{name} 没被完整提取，实际得到 {names} —— "
        "名册里的名字不该交给 jieba 猜"
    )
    # 被吞掉姓氏的那种会产出半截名字，不能出现
    for part in names:
        if part != name and part in name:
            pytest.fail(f"出现了半截名字 {part!r}（{name} 被切开了）")


def test_roster_name_in_bare_text() -> None:
    """只有名字本身时也要对（最简场景）。"""
    register_roster(["陈柏林"])
    ents = extract_entities("陈柏林")
    assert [e.name for e in ents] == ["陈柏林"]
    assert ents[0].kind == "person"


def test_longest_match_wins() -> None:
    """名册里同时有 `柏林` 和 `陈柏林` 时，`陈柏林` 优先。

    **这是挖掉逻辑的核心测试**：不挖掉命中的片段，jieba 会在剩余文本里
    再抽一次「柏林」，同一个人变成两条记录。
    """
    register_roster(["柏林", "陈柏林"])
    ents = extract_entities("陈柏林和柏林不是一个人")
    names = [e.name for e in ents]
    assert "陈柏林" in names, f"最长优先失效：{names}"
    assert names.count("陈柏林") == 1, f"重复提取了：{names}"
    assert "柏林" in names, f"独立的「柏林」没被保留：{names}"


def test_multiple_roster_names_in_one_sentence() -> None:
    """一句话里多个名册名字都要提取。"""
    register_roster(["陈柏林", "刘岳", "王洁"])
    ents = extract_entities("陈柏林问刘岳，王洁说私有云多一点")
    names = {e.name for e in ents}
    assert {"陈柏林", "刘岳", "王洁"} <= names, f"漏了：{names}"


# ============================================================ 不能破坏原有行为


def test_empty_roster_keeps_original_behavior() -> None:
    """名册为空时，行为必须和改动前**完全一致**。

    `陈柏林` → `柏林` 是 jieba 的问题，本 spec 不修（修 jieba 要穷举
    「姓+什么词」，永远有漏网的）。但如果名册机制本身改变了无名单时的行为，
    那就是引入了回归。
    """
    reset_roster()
    ents = extract_entities("陈柏林")
    # 改动前就是「柏林」—— 这里断言的是「没变化」，不是「正确」
    assert [e.name for e in ents] == ["柏林"]


def test_names_outside_roster_still_use_jieba() -> None:
    """名册外的名字仍然走 jieba，不能被名册机制挡住。"""
    register_roster(["陈柏林"])  # 只有陈柏林
    for text, expect in [
        ("欧阳锋说了一句话", "欧阳锋"),
        ("诸葛亮提了个建议", "诸葛亮"),
    ]:
        ents = extract_entities(text)
        assert expect in [e.name for e in ents], (
            f"{expect} 丢了 —— 名册不该干扰名册外的名字"
        )


def test_reset_clears_roster() -> None:
    """`reset_roster()` 之后回到无名单状态。"""
    register_roster(["陈柏林"])
    assert "陈柏林" in roster_names()
    reset_roster()
    assert roster_names() == frozenset()
    assert [e.name for e in extract_entities("陈柏林")] == ["柏林"]


# ============================================================ 注册函数的边界


def test_register_returns_new_count() -> None:
    """返回的是**新增**数量，重复登记不重复计。"""
    assert register_roster(["陈柏林", "刘岳"]) == 2
    assert register_roster(["陈柏林", "王洁"]) == 1  # 陈柏林重复


def test_register_ignores_single_char_and_blank() -> None:
    """单字和空串不收 —— 单字会误伤大量普通词。"""
    assert register_roster(["陈", "", "  ", "柏林"]) == 1
    assert roster_names() == frozenset({"柏林"})


def test_register_strips_whitespace() -> None:
    """前后空白要去掉（数据源可能带空格）。"""
    register_roster(["  陈柏林  "])
    assert "陈柏林" in roster_names()


# ====================================== 名册准入：品牌 / 未知名不冒充「人」
#
# `周黑鸭` 和 `陈柏林` 是同一个问题的正反面：
# 字符串本身不携带「是不是人」的信息，jieba 的 `nr` 只是猜。
# 有权威名册时，`person` 只认名册。


@pytest.mark.parametrize(
    "text",
    [
        "周大福今天开业",      # jieba: 周大福/nr  —— 珠宝品牌
        "王老吉的广告不错",    # jieba: 王老吉/nr  —— 饮料品牌
        "我买了周黑鸭",        # jieba: 周/nr + 黑 + 鸭 —— 食品品牌
    ],
)
def test_brand_names_not_person_when_roster_present(text: str) -> None:
    """名册存在时，品牌名不得被当成 `Person`。"""
    register_roster(["王泳", "刘佳"])
    ents = [(e.name, e.kind) for e in extract_entities(text)]
    assert all(k != "person" for _, k in ents), f"品牌被当成人：{ents}"


def test_roster_member_still_person() -> None:
    """名册里的人在同样条件下仍是 `person` —— 准入不能过紧。"""
    register_roster(["王泳"])
    assert [(e.name, e.kind) for e in extract_entities("王泳说这个方案可以")] == [
        ("王泳", "person")
    ]


def test_unknown_name_demoted_to_topic() -> None:
    """名册外的名字降级为 `topic`：信息保留，但不冒充人。"""
    register_roster(["王泳"])
    kinds = {e.name: e.kind for e in extract_entities("欧阳锋说了一句话")}
    assert kinds.get("欧阳锋") == "topic"


def test_brand_without_roster_keeps_old_behaviour() -> None:
    """无名册时保持旧行为 —— 这是已知缺陷，不是回归。

    单条 `memory_remember` 没有名册，此时只能靠 jieba 猜；
    035 的范围边界就在这里，不假装修好了。
    """
    reset_roster()
    kinds = [(e.name, e.kind) for e in extract_entities("周大福今天开业")]
    assert ("周大福", "person") in kinds
