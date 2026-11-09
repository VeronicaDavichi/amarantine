"""B4 规则层实体抽取。

这套规则的核心难点：**中文没有空格，没有词边界**。

任何 `[\\u4e00-\\u9fa5]{2,12}?项目` 的写法都会从句子开头一路吞到后缀，
于是 `张三决定项目A` 抽出 `张三决定项目` —— 一个根本不存在的东西。

解法是反过来：**从后缀位置往前扫，撞到谓语/助词就停手**。
"""

from __future__ import annotations

import pytest

from memory.services.labeler import extract_actions, extract_entities, label_by_rules


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
    """`腾讯公司` 要抽成 org，`上海` 顺手抽成 place。

    正则版会漏掉 `上海`（它只认 `市/区/路/街` 后缀，裸城市名抓不到）；
    jieba 有 `ns` 标注，能直接认出来。
    """
    names = _names("我在上海出差见了腾讯公司的产品团队")
    assert ("腾讯公司", "org") in names
    assert ("上海", "place") in names


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


# ---------------- jieba 分词路径 ----------------
#
# 下面这些是正则**做不到**的。jieba 有词边界，正则没有 ——
# 正则会把 `张三让李四去做项目A` 从「张三」一路吞到「项目」。


def test_two_persons_with_preposition() -> None:
    """`张三让李四去做项目A` —— 两个人和一个项目全要拿到。

    正则版这里必挂：姓氏表里 `让` 不在停用字表，会抽出 `张三让李四`。
    """
    names = _names("张三让李四去做项目A")
    assert ("张三", "person") in names
    assert ("李四", "person") in names
    assert ("项目A", "topic") in names


def test_person_after_verb() -> None:
    """`告诉张三` —— 人名不在句首也不在常见介词后，正则抓不到。"""
    assert ("张三", "person") in _names("告诉张三这个月要交报告")


def test_person_tail_function_word_trimmed() -> None:
    """jieba 会把 `张三让` 标成一个 nr，尾部函数词要裁掉。"""
    names = [n for n, k in _names("张三让李四去做项目A") if k == "person"]
    assert "张三让" not in names
    assert "张三" in names


def test_single_char_person_rejected() -> None:
    """`周会上` 被切成 `周`(nr) + `会上`(t) —— 单字人名一律不要。"""
    persons = [n for n, k in _names("周会上讨论了明年的预算分配方案") if k == "person"]
    assert persons == []


def test_city_without_suffix_extracted() -> None:
    """裸城市名（`北京` / `上海`）没有 `市/区/路` 后缀，只有 jieba 的 ns 能认。"""
    assert ("北京", "place") in _names("王五和赵六在北京见面")


@pytest.mark.parametrize(
    "text",
    [
        "周会上讨论了明年的预算分配方案",
        "今天背了五十个单词",
        "老板批评进度太慢",
        "时间定在下周三",
    ],
)
def test_jieba_also_has_no_ghost_persons(text: str) -> None:
    """换成分词也不能凭空造人。"""
    persons = [n for n, k in _names(text) if k == "person"]
    assert persons == [], f"不该抽出人名，实际 {persons}"


# ---------------- 降级路径 ----------------

def test_fallback_when_jieba_unavailable(monkeypatch) -> None:
    """jieba 不可用时必须降级到正则，不能崩。

    抽取失败会直接阻断写卡，所以这条是硬要求。
    """
    from memory.services import labeler as L

    monkeypatch.setattr(L, "_JIEBA_OK", False)
    names = L.extract_entities("张三决定项目A延期到六月")
    got = [(e.name, e.kind) for e in names]
    assert ("张三", "person") in got
    assert ("项目A", "topic") in got


def test_jieba_probe_is_cached(monkeypatch) -> None:
    """探测结果要缓存，否则每次抽取都去 import。"""
    from memory.services import labeler as L

    L.reset_jieba_probe()
    first = L._jieba_ready()
    second = L._jieba_ready()
    assert first is second
    assert L._JIEBA_OK is not None


# ---------------- 动作抽取 ----------------


def _acts(text: str) -> list[tuple[str, str | None, bool]]:
    return [(a.verb, a.patient, a.primary) for a in extract_actions(text)]


def test_two_actions_with_primary() -> None:
    """`张三决定项目A延期到六月` 有两个动作：决定（主）+ 延期（嵌套）。"""
    assert _acts("张三决定项目A延期到六月") == [
        ("决定", "项目A", True),
        ("延期", "项目A", False),
    ]


def test_exactly_one_primary() -> None:
    acts = extract_actions("张三决定项目A延期到六月")
    assert sum(1 for a in acts if a.primary) == 1
    assert acts[0].primary is True


def test_verb_synonyms_are_merged() -> None:
    """`推迟` / `延后` / `延期` 必须归并成同一个 verb。

    不归并的话图上就是三个孤立节点，「哪些事被延过」这种聚合查询直接废掉。
    """
    for surface in ("推迟", "延后", "延期", "顺延"):
        verbs = [a.verb for a in extract_actions(f"项目A{surface}到下个月")]
        assert verbs == ["延期"], f"{surface} 应归一化成「延期」，实际 {verbs}"


def test_patient_from_before_verb() -> None:
    """中文常把宾语话题化到动词前面：`项目A延期了` 和 `延期项目A` 同义。"""
    acts = _acts("项目A推迟到下个月")
    assert acts == [("延期", "项目A", True)]


def test_no_duplicate_when_spans_overlap() -> None:
    """`确定了` 里 `确定` 和 `定了` 都命中，不能同时抽出两个动作。"""
    verbs = [a.verb for a in extract_actions("李四确定了新项目B的启动时间")]
    assert verbs.count("确定") == 1
    assert "决定" not in verbs, f"`定了` 是 `确定` 的一部分，不该单独算：{verbs}"


def test_action_without_patient() -> None:
    acts = _acts("老板批评进度太慢")
    assert acts == [("批评", None, True)]


def test_no_actions_returns_empty() -> None:
    assert extract_actions("今天天气不错") == []


def test_actions_capped() -> None:
    from memory.services.labeler import MAX_ACTIONS

    text = "决定确定讨论开始完成取消同意拒绝修改新增删除汇报"
    assert len(extract_actions(text)) <= MAX_ACTIONS


def test_label_by_rules_includes_actions() -> None:
    result = label_by_rules("张三决定项目A延期到六月")
    verbs = [a.verb for a in result.actions]
    assert "决定" in verbs
    assert "延期" in verbs


# ---------------- 半截词：名词后面紧贴的 nr 不是人 ----------------


def test_fragment_after_noun_is_not_person() -> None:
    """`王者荣耀` 被切成 `王者`(n) + `荣耀`(nr)，`荣耀` 不能冒充人。

    人名前面不会紧贴名词（`张三让` 是动词、`王五和` 是连词、`跟王老师` 是介词），
    所以「前一个 token 是名词」就是词被切开的信号 —— 降级为 `topic`。
    """
    kinds = {e.name: e.kind for e in extract_entities("王者荣耀这个游戏挺好玩的")}
    assert kinds.get("荣耀") == "topic", f"半截词又冒充人了：{kinds}"


def test_fragment_after_noun_with_and() -> None:
    """`昨天和王者荣耀一起开的会` 同样要降级。"""
    kinds = {e.name: e.kind for e in extract_entities("昨天和王者荣耀一起开的会")}
    assert kinds.get("荣耀") == "topic", f"半截词又冒充人了：{kinds}"


def test_real_name_after_verb_still_person() -> None:
    """守卫不能误伤：动词/连词后面的人名仍是人。"""
    kinds = {e.name: e.kind for e in extract_entities("张三让李四去做项目A")}
    assert kinds.get("张三") == "person"
    assert kinds.get("李四") == "person"
