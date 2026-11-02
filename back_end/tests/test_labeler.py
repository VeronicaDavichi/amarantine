"""B4 规则层实体抽取。

这套规则的核心难点：**中文没有空格，没有词边界**。

任何 `[\\u4e00-\\u9fa5]{2,12}?项目` 的写法都会从句子开头一路吞到后缀，
于是 `张三决定项目A` 抽出 `张三决定项目` —— 一个根本不存在的东西。

解法是反过来：**从后缀位置往前扫，撞到谓语/助词就停手**。
"""

from __future__ import annotations

import pytest

from memory.services.labeler import extract_entities, label_by_rules


def _names(text: str) -> list[tuple[str, str]]:
    return [(e.name, e.kind) for e in extract_entities(text)]


# ---------------- 核心用例：用户明确指出过的 ----------------


def test_zhangsan_and_project_a() -> None:
    """`张三决定项目A延期到六月` → 张三(人) + 项目A(话题)。

    这是整套规则的目标用例。修之前抽出来的是 `张三决定项目`。
    """
    assert _names("张三决定项目A延期到六月") == [
        ("张三", "person"),
        ("项目A", "topic"),
    ]


def test_person_not_swallowing_verb() -> None:
    """人名必须在谓语前停手，不能吃成 `张三决定`。"""
    names = [n for n, _ in _names("张三决定项目A延期到六月")]
    assert "张三决定" not in names
    assert "张三" in names


def test_project_not_swallowing_person() -> None:
    """项目名不能从句子开头吞，不能抽出 `张三决定项目`。"""
    names = [n for n, _ in _names("张三决定项目A延期到六月")]
    assert "张三决定项目" not in names
    assert "项目A" in names


def test_lisi_new_project() -> None:
    assert _names("李四确定了新项目B的启动时间") == [
        ("李四", "person"),
        ("项目B", "topic"),
    ]


# ---------------- 不能误判成人 ----------------


@pytest.mark.parametrize(
    "text",
    [
        "周会上讨论了明年的预算分配方案",
        "今天背了五十个单词",
        "老板批评进度太慢",
        "这个方案还需要再改",
        "时间定在下周三",
    ],
)
def test_common_words_are_not_persons(text: str) -> None:
    """姓氏表里有`方`/`时`/`单`/`公`这些常用字，位置约束必须挡住它们。

    修之前 `方案` / `时间` / `单词` / `太慢` 全被当成 person —— 图里会多出
    一堆不存在的人。
    """
    persons = [n for n, k in _names(text) if k == "person"]
    assert persons == [], f"不该抽出人名，实际 {persons}"


def test_person_at_sentence_start() -> None:
    assert ("张三", "person") in _names("张三来了")


def test_person_after_conjunction() -> None:
    """`和`/`与`/`跟` 后面可以接人名。"""
    names = _names("王五和赵六在北京见面")
    assert ("王五", "person") in names
    assert ("赵六", "person") in names


def test_no_person_in_middle_of_noun() -> None:
    """`上海` 的 `上`、`公司` 的 `公` 都在姓氏表里，但都不在允许位置。"""
    names = [n for n, k in _names("我去了上海出差") if k == "person"]
    assert names == []


# ---------------- 机构 / 地点 ----------------


def test_org_extracted_by_suffix() -> None:
    assert _names("我在上海出差见了腾讯公司的产品团队") == [
        ("腾讯公司", "org"),
    ]


def test_org_not_swallowing_prefix() -> None:
    """`腾讯公司` 不能抽成 `见了腾讯公司`。"""
    names = [n for n, _ in _names("我在上海出差见了腾讯公司的产品团队")]
    assert "见了腾讯公司" not in names


# ---------------- 多实体 ----------------


def test_two_persons_and_project() -> None:
    assert _names("张三和李四讨论了项目A的排期") == [
        ("张三", "person"),
        ("李四", "person"),
        ("项目A", "topic"),
    ]


def test_no_entities_returns_empty() -> None:
    assert _names("今天天气不错") == []


# ---------------- 与打标联动 ----------------


def test_label_by_rules_includes_entities() -> None:
    result = label_by_rules("张三决定项目A延期到六月")
    names = [(e.name, e.kind) for e in result.entities]
    assert ("张三", "person") in names
    assert ("项目A", "topic") in names


def test_label_by_rules_types_still_work() -> None:
    result = label_by_rules("张三决定项目A延期到六月")
    assert "decision" in result.types
    assert result.types


def test_person_excluded_from_topic_entities() -> None:
    """同一个串不能既是人又是项目 —— 图里会分裂成两个节点。"""
    ents = _names("张三决定项目A延期到六月")
    names = [n for n, _ in ents]
    persons = [n for n, k in ents if k == "person"]
    topics = [n for n, k in ents if k == "topic"]
    assert not (set(persons) & set(topics)), f"{names} 里有人同时是话题"


# ---------------- 内存安全 ----------------


def test_long_text_does_not_hang() -> None:
    text = "张三决定项目A延期到六月" * 200
    result = extract_entities(text)
    assert isinstance(result, list)
