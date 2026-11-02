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


# ================================================================
# 左值校验
#
# 原来只校验右侧（canonical），左侧只要长度 2–12 就放行。
# `三哥主打的就是有声有色有味` 于是匹配成
# `三哥主打的 -> 有声有色有味`，右边过了判据，
# `(:Alias {name:"三哥主打的"})` 就这样进了图。
#
# 左侧才是最容易脏的一侧：它紧挨等价词，正则 `{2,12}` 会一路吞到谓语。
# ================================================================


def test_verbose_left_side_rejected() -> None:
    """`三哥主打的` 是句子片段，不是名字。"""
    from memory.services.labeler import extract_alias_claims

    claims = extract_alias_claims("三哥主打的就是有声有色有味")
    assert claims == [], f"左值是句片短语，不该产出声明: {claims}"


def test_verbose_left_side_does_not_pollute_entities() -> None:
    """连右侧也不能因为这个假声明进图。"""
    from memory.services.labeler import extract_entities

    names = {e.name for e in extract_entities("三哥主打的就是有声有色有味")}
    assert "有声有色有味" not in names, f"描述性右值被当成实体了: {names}"
    assert "三哥主打的" not in names


def test_left_side_ending_with_de_rejected() -> None:
    """以「的」结尾的一律不是名字。"""
    from memory.services.labeler import extract_alias_claims

    for text in ("前面说的就是这个人", "刚才提到的就是那个", "张三说的就是李四"):
        claims = extract_alias_claims(text)
        assert not any(c.alias.endswith("的") for c in claims), (
            f"{text} 抽出了以「的」结尾的别名: {claims}"
        )


def test_left_side_starting_with_pronoun_rejected() -> None:
    """代词开头的是句子片段。"""
    from memory.services.labeler import extract_alias_claims

    claims = extract_alias_claims("我刚才说的那个东西就是我之前提的那个")
    assert claims == [], f"代词开头的不是名字: {claims}"


def test_long_left_side_rejected() -> None:
    """超过 6 个字的一律不收（名字没那么长）。"""
    from memory.services.labeler import _is_clean_name

    assert not _is_clean_name("这个特别长的东西名称")
    assert _is_clean_name("豆包")


@pytest.mark.parametrize(
    "text,alias,canonical",
    [
        ("糖包就是豆包", "糖包", "豆包"),
        ("李四也叫作小李", "李四", "小李"),
        ("张三又称老张", "张三", "老张"),
        ("项目A就是项目B", "项目A", "项目B"),
    ],
)
def test_clean_left_sides_still_accepted(
    text: str, alias: str, canonical: str
) -> None:
    """收紧左值不能把正常声明也误杀。"""
    from memory.services.labeler import extract_alias_claims

    pairs = [(c.alias, c.canonical) for c in extract_alias_claims(text)]
    assert (alias, canonical) in pairs, f"{text} 的正常声明被误杀了: {pairs}"


def test_clean_name_rejects_descriptive_markers() -> None:
    from memory.services.labeler import _is_clean_name

    for bad in ("那个东西", "这种人", "最好之一", "他们家"):
        assert not _is_clean_name(bad), f"{bad} 不该通过"


def test_clean_name_accepts_short_products() -> None:
    """`豆包` 这种产品名要能过 —— 别名声明就是为它开的。"""
    from memory.services.labeler import _is_clean_name

    assert _is_clean_name("豆包")
    assert _is_clean_name("糖包")


# ---------------- 「X 人」：名词性类词缀，降置信度而不是拒收 ----------------
#
# `人` 曾经直接在 `_DESCRIPTIVE_MARKERS` 里，一刀切拦掉所有带「人」的串。
# 结果 `三哥就是印度人` 抽得出 claim，但 `append_alias_edges` 用
# `_is_clean_name(canonical)` 过滤时把它丢掉了 —— 边一条建不出来，
# 而且没有任何日志。
#
# 汉典把 `人` 列为名词，举证里就有 主持人/介绍人/证人/强人/恩人 ——
# `X人` 是**能产构词**（类词缀），造出来的是合法名词。
# 所以：`X人` 一律放行进图，评价性的（老实人）只降置信度。


@pytest.mark.parametrize(
    "name",
    [
        "印度人",   # 用户报的那个
        "中国人",
        "上海人",
        "东北人",
        "外国人",   # 「外国」是地点性质，不是评价
        "主持人",   # 名词 + 人：职业
        "负责人",
        "介绍人",
        "强人",     # 汉典举证里有，不该被形容词表误杀
        "恩人",
    ],
)
def test_person_noun_is_admitted_and_exact(name: str) -> None:
    """名词 + 「人」是名词短语 → 能建边，且置信度 exact。"""
    from memory.services.labeler import _alias_label, _is_clean_name

    assert _is_clean_name(name), f"{name} 是名词短语，不该被拒收"
    assert _alias_label(name) == "exact", f"{name} 该是 exact"


@pytest.mark.parametrize(
    "desc",
    ["老实人", "聪明人", "坏人", "好人", "懒人", "陌生人"],
)
def test_adjectival_ren_admitted_but_downgraded(desc: str) -> None:
    """形容词 + 「人」是评价 —— **不再拒收**，只降置信度。

    拒收 = 边建不出来且没有日志，正是 `印度人` 那个 bug 的形态。
    降级 = 边照建、置信度 0.4、召回不当强证据、可 rebind 清理。
    宁可要个低置信度实体，也不要再出现「图上没有还查不出为什么」。
    """
    from memory.services.labeler import _alias_label, _is_clean_name

    assert _is_clean_name(desc), f"{desc} 不该被拒收（拒收是静默的）"
    assert _alias_label(desc) == "uncertain", f"{desc} 该降级"


@pytest.mark.parametrize("desc", ["什么人", "那种人", "这种人"])
def test_ren_with_determiner_still_rejected(desc: str) -> None:
    """限定词开头的还是要拦 —— 那是多字标记抓的，不是「人」抓的。"""
    from memory.services.labeler import _is_clean_name

    assert not _is_clean_name(desc), f"{desc} 是句子片段"


def test_sange_is_indian_claim_is_created_and_clean() -> None:
    """`三哥就是印度人` 要能抽出 claim，且右值过得了建边判据。

    这两步原来各断一次：claim 抽得出来，但
    `graph_outbox.append_alias_edges` 的 `_is_clean_name(canonical)`
    过滤把它丢了 —— 所以「claim 存在」不等于「边建得出来」，
    两个断言都得有。
    """
    from memory.services.labeler import _is_clean_name

    claims = extract_alias_claims("三哥就是印度人")
    assert len(claims) == 1, claims
    c = claims[0]
    assert (c.alias, c.canonical) == ("三哥", "印度人")
    # 用户用「就是」说死了等价关系 → exact
    assert c.label == "exact"
    assert _is_clean_name(c.canonical), "右值必须过得了建边判据"


def test_sange_is_indian_actually_reaches_graph_items() -> None:
    """真的走到 `append_alias_edges`，确认 item 列表里有它。

    不走这一步的话，改完判据也不知道边到底建没建 —— 过滤发生在
    函数内部，只看 claim 是看不出来的。
    """
    from typing import Any

    from memory.services import graph_outbox as GO

    class _Session:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def run(self, cypher: str, **params: Any) -> Any:
            self.calls.append((cypher, params))

            class _R:
                def __iter__(self):
                    return iter([])

                def consume(self):
                    return None

            return _R()

    class _Driver:
        def __init__(self) -> None:
            self.s = _Session()

        def session(self):
            return self.s

        def close(self) -> None:
            pass

    class _Cfg:
        NEO4J_URI = "bolt://fake:7687"
        NEO4J_USER = "neo4j"
        NEO4J_PASSWORD = "pw"

    drv = _Driver()
    claims = extract_alias_claims("三哥就是印度人")
    written = GO.append_alias_edges(
        "evt_0000000000000001",
        "work",
        claims,
        settings=_Cfg(),
        created_at="2026-11-02T08:00:00+00:00",
        driver=drv,
    )
    assert written == 1, "`印度人` 这条边必须真的建出来"
    params = drv.s.calls[0][1]
    aliases = [item["alias"] for item in params["aliases"]]
    canonicals = [item["canonical"] for item in params["aliases"]]
    assert aliases == ["三哥"]
    assert canonicals == ["印度人"]


def test_description_reaches_graph_with_low_confidence() -> None:
    """`小明就是老实人` —— claim 留着，边也建，但标 uncertain。

    以前这里断言「不能建边」。改成能建，是因为硬拒的代价太大：
    边建不出来**而且没有任何日志**，正是 `印度人` 那个 bug 的形态。
    现在让数据进来，用置信度表达质量，可查、可降权、可撤销。
    """
    from memory.services.labeler import _alias_label, _is_clean_name

    claims = extract_alias_claims("小明就是老实人")
    assert len(claims) == 1
    assert claims[0].canonical == "老实人"
    assert claims[0].label == "uncertain", "评价性右值要标存疑"
    assert _is_clean_name(claims[0].canonical), "边要建得出来（不能静默丢）"
    assert _alias_label(claims[0].canonical) == "uncertain"


# ---------------- 单字只做位置检查，包含检查只用于多字词 ----------------
#
# 依据：中央研究院《詞類標記表》的封闭类/开放类划分，以及吕叔湘
# 《說自由和粘著》的自由语素/粘着语素区分。`个/位/家/种/人` 都是
# 自由语素 —— 能单用成词，也能构词，位置不固定。放进包含式匹配
# 必然在名词里误伤。


def test_no_single_char_markers() -> None:
    """看门狗：`_DESCRIPTIVE_MARKERS` 里不许出现单字。

    这条断言就是防止以后有人再往集合里塞 `家`/`个`/`位` 之类。
    单字要走位置检查（`_LEADING_CHARS` / 结尾助词表），不能走包含。
    """
    from memory.services.labeler import _DESCRIPTIVE_MARKERS

    bad = sorted(m for m in _DESCRIPTIVE_MARKERS if len(m) < 2)
    assert not bad, f"这些单字不该做包含式标记: {bad}"


@pytest.mark.parametrize(
    "name",
    [
        # 单字「家」误伤的名词（类词缀 / 普通名词）
        "科学家", "作家", "画家", "国家", "行家", "专家", "厂家",
        # 单字「位」误伤、但不以量词起头的
        "单位",
        "人民",
    ],
)
def test_nouns_hit_by_single_char_markers_are_admitted(name: str) -> None:
    """这些名词曾被单字标记的包含式匹配误伤，现在放行。"""
    from memory.services.labeler import _alias_label, _is_clean_name

    assert _is_clean_name(name), f"{name} 是名词，不该被拒收"
    assert _alias_label(name) == "exact", f"{name} 该是 exact"


@pytest.mark.parametrize("noun", ["位置", "个别", "个体", "种类", "种族"])
def test_nouns_starting_with_measure_word_rejected(noun: str) -> None:
    """以量词起头的名词会被**开头位置检查**拦掉 —— 这是有意的。

    区别在于：`位置`/`种类` 这些词本身是名词，但它们不可能是**名字**。
    量词打头出现在别名位置，只能是 `就是` 后面紧跟量词造成的半截短语
    （`就是**个**老实人` → `个老实人`）。丢掉它们没有损失，
    而 `科学家`/`单位` 这类不以量词起头的照常放行。
    """
    from memory.services.labeler import _is_clean_name

    assert not _is_clean_name(noun), f"{noun} 以量词起头，不该当名字"


@pytest.mark.parametrize(
    "fragment",
    ["个老实人", "位老师", "种人"],
)
def test_measure_word_leading_fragment_rejected(fragment: str) -> None:
    """`小明就是个老实人` 会被捕获成 `个老实人` —— 半截短语，不是名字。

    这是量词走**开头检查**而不是包含检查的直接原因：以前 `个` 在包含式
    集合里能拦住它，去掉单字标记后必须补一条位置规则，否则这个口子
    会静默打开。
    """
    from memory.services.labeler import _is_clean_name

    assert not _is_clean_name(fragment), f"{fragment} 是半截短语"


@pytest.mark.parametrize(
    "junk",
    ["家里", "家人", "家门口", "家庭", "家中", "家族"],
)
def test_jia_compounds_are_descriptions(junk: str) -> None:
    """「家」的实词实现 —— 用具体词拦，不用单字规则。

    `家` 单用是量词，在 `科学家` 里是类词缀。想拦 `家里` 就该写 `家里`，
    而不是把 `家` 拉黑（那会连 `科学家` 一起打掉）。
    """
    from memory.services.labeler import _is_clean_name

    assert not _is_clean_name(junk), f"{junk} 是普通名词，不是名字"


@pytest.mark.parametrize(
    "junk",
    ["一个AI", "某种", "那位", "各位", "每个", "某个", "整个", "这种", "那种"],
)
def test_double_char_determiners_still_rejected(junk: str) -> None:
    """单字量词换成双字实现后，原来的拦截能力不能丢。"""
    from memory.services.labeler import _is_clean_name

    assert not _is_clean_name(junk), f"{junk} 是指示/数量词"


@pytest.mark.parametrize("junk", ["最近", "很好", "太快", "更高", "不错"])
def test_leading_adverb_rejected(junk: str) -> None:
    """单字副词走**开头检查**，不走包含检查。

    这是位置检查的好处：`最` 做包含会命中 `最近`/`最终`，
    做开头检查只命中真正的描述。
    """
    from memory.services.labeler import _is_clean_name

    assert not _is_clean_name(junk), f"{junk} 以副词开头，是描述"
