"""别名声明的抽取与置信度分级。

核心需求：**一个人可能有多种称呼，一种称呼也可能对应多个人。**

置信度的判据是「这句话里有几分显式的身份锚定」：

- `糖包就是豆包`               → exact     用户直接给了等价关系
- `糖包就是那个AI里最弱智的`    → uncertain 得先知道「最弱智的AI」是谁
- `三哥` 既能指印度人也能指老三  → 一词多义，别名不是一对一
"""

from __future__ import annotations

import pytest

from memory.services.labeler import (
    _looks_like_name,
    extract_alias_claims,
    label_by_rules,
)


def _one(text: str):
    claims = extract_alias_claims(text)
    assert len(claims) == 1, f"应抽出 1 条，实际 {claims}"
    return claims[0]


# ---------------- 显式等价 → exact ----------------

def test_explicit_equivalence_is_exact() -> None:
    """用户说「糖包就是豆包」—— 最关键的那档，不用任何推理。"""
    c = _one("糖包就是豆包")
    assert c.alias == "糖包"
    assert c.canonical == "豆包"
    assert c.label == "exact"
    assert c.source == "user_declared"


@pytest.mark.parametrize(
    "text,alias,canonical",
    [
        ("小张也叫张三", "小张", "张三"),
        ("张三又称张总", "张三", "张总"),
        ("李四也叫作小李", "李四", "小李"),
        ("老李亦称李工", "老李", "李工"),
        ("豆包又叫糖包", "豆包", "糖包"),
    ],
)
def test_all_equivalence_markers(text: str, alias: str, canonical: str) -> None:
    """这几种说法都是显式等价，不能只认「就是」。"""
    c = _one(text)
    assert (c.alias, c.canonical) == (alias, canonical)
    assert c.label == "exact"


# ---------------- 描述性 → uncertain ----------------

def test_descriptive_right_side_is_uncertain() -> None:
    """`糖包就是那个AI里最弱智的` —— 右边是描述不是名字。

    要落到「豆包」得先知道「最弱智的AI」是谁，这一步会错，
    所以只能标存疑，不能当事实。
    """
    c = _one("糖包就是那个ai中最弱智的")
    assert c.label == "uncertain"


def test_description_does_not_become_alias_node() -> None:
    """描述性串不能建图边 —— 否则以后查「糖包」会拉出一个叫
    「那个ai中最弱智的」的人。"""
    c = _one("糖包就是那个ai中最弱智的")
    assert not _looks_like_name(c.canonical)


def test_short_clean_name_can_be_linked() -> None:
    """`豆包` 这种短而干净的名字可以建边。"""
    assert _looks_like_name("豆包")
    assert _looks_like_name("张三")


def test_prefix_is_stripped_but_label_uses_raw() -> None:
    """裁剪前缀会把 `那个` 削掉，但判置信度必须用**原始串**。

    否则 `我说的豆包就是那个国产大模型` 会被误判成 exact。
    """
    c = _one("我说的豆包就是那个国产大模型")
    assert c.alias == "豆包", "左侧的「我说的」要裁掉"
    assert c.label == "uncertain", "原始串里有「那个」，不能算 exact"


# ---------------- 不该被当成别名 ----------------

@pytest.mark.parametrize(
    "text",
    [
        "张三是个好人",
        "项目要延期到六月",
        "我觉得这个方案不行",
        "今天天气不错",
    ],
)
def test_plain_sentence_is_not_alias(text: str) -> None:
    """只认显式等价词。`A 是 B` 不算 —— `张三是好人` 不是别名声明。"""
    assert extract_alias_claims(text) == []


def test_self_equivalence_skipped() -> None:
    """`张三就是张三` 没有信息量，不该产生边。"""
    assert extract_alias_claims("张三就是张三") == []


def test_no_duplicate_claims() -> None:
    claims = extract_alias_claims("小张也叫张三，小张也叫张三")
    assert len(claims) == 1


# ---------------- 接进打标 ----------------

def test_label_by_rules_includes_alias_claims() -> None:
    result = label_by_rules("小张也叫张三")
    assert len(result.alias_claims) == 1
    assert result.alias_claims[0].alias == "小张"


def test_alias_claims_do_not_affect_types() -> None:
    """别名声明不该改变事件分类。"""
    a = label_by_rules("张三决定项目A延期")
    b = label_by_rules("小张也叫张三")
    assert a.types and b.types


def test_multiple_claims_in_one_sentence() -> None:
    """一句话里可以有多组别名。"""
    claims = extract_alias_claims("小张也叫张三，老李又称李总")
    assert len(claims) == 2
    got = {(c.alias, c.canonical) for c in claims}
    assert ("小张", "张三") in got
    assert ("老李", "李总") in got


# ================================================================
# 别名声明两侧的名字要进实体列表
#
# 背景：`豆包` 这类产品名被 jieba 标成普通名词 `n`，任何合并规则都收不到。
# 但实体纠错机制目前约等于不存在（没有 API 能删单个实体节点），
# 所以规则层宁可漏 —— 只有别名声明这种**用户亲口断言**的场景才补收。
# ================================================================


def test_alias_left_side_becomes_entity() -> None:
    """`糖包` 是 jieba 的普通名词，靠别名声明才进得来。"""
    from memory.services.labeler import extract_entities

    names = {e.name for e in extract_entities("糖包就是豆包")}
    assert "糖包" in names, f"别名声明的左值应进实体，实际 {names}"
    assert "豆包" in names


def test_descriptive_right_side_not_entity() -> None:
    """描述性右值不是名字，不该进实体列表。"""
    from memory.services.labeler import extract_entities

    names = {e.name for e in extract_entities("糖包就是那个AI里最弱智的")}
    assert "糖包" in names
    assert not any("最弱智" in n for n in names), f"描述被当成实体了: {names}"


def test_plain_sentence_still_misses_product_names() -> None:
    """**故意保留的漏**：`豆包是我们组用的AI助手` 抽不到「豆包」。

    这不是 bug，是取舍：没有实体纠错能力，就不能靠「句首名词」去猜。
    哪天加了实体纠错，这条测试应该跟着改。
    """
    from memory.services.labeler import extract_entities

    names = {e.name for e in extract_entities("豆包是我们组用的AI助手")}
    assert "豆包" not in names, (
        "如果这条开始通过了，说明实体抽取已放开精度 —— "
        "请先确认实体纠错能力已经存在，再改这条测试"
    )


def test_alias_entities_are_topic_not_person() -> None:
    """声明说的是「两个称呼等价」，分不出人还是物 —— 标 topic。

    凭空把「豆包」认成一个人比漏掉更糟。
    """
    from memory.services.labeler import extract_entities

    kinds = {e.name: e.kind for e in extract_entities("糖包就是豆包")}
    assert kinds["糖包"] == "topic"
    assert kinds["豆包"] == "topic"


def test_alias_entities_do_not_duplicate() -> None:
    """`张三也叫张三` 不该冒出两个张三。"""
    from memory.services.labeler import extract_entities

    names = [e.name for e in extract_entities("小张也叫张三")]
    assert len(names) == len(set(names)), f"实体重复: {names}"
