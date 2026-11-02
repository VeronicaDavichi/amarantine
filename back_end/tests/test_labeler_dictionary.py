"""分词词典：静态排行称谓 + 启动时 warm-up + 连词剥离。

## 为什么这组测试单独一个文件

它们测的是**进程级全局状态**（jieba 词典），跟纯函数的抽取测试不同。
`conftest.py` 里有一个 autouse fixture 在每个测试后调 `reset_dictionary()`，
否则测试之间会互相污染 —— `test_dual_run` 灌的「项目A」会让
`test_labeler` 把「项目A」抽成人，而且是单跑通过、全量失败那种最难查的形态。
"""

from __future__ import annotations

from memory.services.labeler import (
    _rank_appellations,
    _strip_leading_conj,
    extract_entities,
    load_entity_dictionary,
)


def _names(text: str) -> list[tuple[str, str]]:
    return [(e.name, e.kind) for e in extract_entities(text)]


# ---------------- 排行称谓静态词典 ----------------

def test_rank_appellations_cover_common_forms() -> None:
    words = _rank_appellations()
    for expected in ("三哥", "二哥", "四妹", "三叔", "一姐"):
        assert expected in words


def test_rank_appellations_have_no_obvious_junk() -> None:
    """`三公` / `一婆` 这类不是称呼，不该在词表里。"""
    words = set(_rank_appellations())
    for junk in ("三公", "一婆", "十哥婆"):
        assert junk not in words


def test_san_ge_extracted_without_warmup() -> None:
    """`三哥是我哥` 原来切成 `三/m` + `哥/n`，整句抽不到人。"""
    assert ("三哥", "person") in _names("三哥是我哥")


def test_san_ge_is_person_not_place() -> None:
    """原来 `三哥主打…` 被标成 `ns`（地名）——「三哥」是称呼，不是地点。"""
    got = _names("三哥主打的就是一个有声有色有味")
    assert ("三哥", "person") in got
    assert not any(name == "三哥" and kind == "place" for name, kind in got)


def test_other_rank_appellations() -> None:
    assert ("二哥", "person") in _names("二哥来了")
    assert ("三叔", "person") in _names("三叔来了")
    assert ("四妹", "person") in _names("四妹在杭州")


# ---------------- 连词剥离 ----------------

def test_leading_conjunction_stripped() -> None:
    """`老李和小王吵架` 原来会抽出 `和小王` ——「和」粘进了名字。"""
    got = [name for name, _kind in _names("老李和小王吵架")]
    assert "小王" in got
    assert "和小王" not in got, "连词不能留在名字里"


def test_conjunction_not_stripped_when_result_too_short() -> None:
    """`和平` 不能剥成 `平`。"""
    assert _strip_leading_conj("和平") == "和平"


def test_conjunction_not_stripped_when_result_is_stop_word() -> None:
    """`同时` 不能剥成 `时`。"""
    assert _strip_leading_conj("同时") == "同时"
    assert _strip_leading_conj("同事") == "同事"


def test_non_conjunction_untouched() -> None:
    assert _strip_leading_conj("张三") == "张三"
    assert _strip_leading_conj("项目A") == "项目A"


# ---------------- 图谱反哺 ----------------

def test_learned_name_is_segmented_as_one_word() -> None:
    """灌进去的名字下次要被当成一个词，不能切碎。"""
    import jieba.posseg as pseg

    # 灌之前：`豆包` 会被拆开
    load_entity_dictionary(names=["豆包"])
    words = [w.word for w in pseg.cut("豆包上线了新的语音功能")]
    assert "豆包" in words, f"灌进词典后应该切成一个词，实际: {words}"


def test_loaded_name_is_extracted() -> None:
    load_entity_dictionary(names=["豆包"])
    got = _names("豆包上线了新的语音功能")
    assert any(name == "豆包" for name, _kind in got), f"实际: {got}"


def test_reset_removes_learned_words() -> None:
    """`reset_dictionary` 必须真的把词撤掉，否则测试之间会污染。"""
    from memory.services.labeler import _DICT_LOADED, reset_dictionary

    load_entity_dictionary(names=["豆包"])
    assert "豆包" in _DICT_LOADED
    reset_dictionary()
    assert "豆包" not in _DICT_LOADED


def test_person_names_tagged_as_nr() -> None:
    """人 → `nr`。标对了后续抽取才能当成人。"""
    load_entity_dictionary(persons=["小明"])
    got = _names("小明去北京")
    assert ("小明", "person") in got, f"实际: {got}"


def test_entity_names_not_tagged_as_person() -> None:
    """物 → `nz`。标成 `nr` 会让「项目A」在下一句话里变成一个人。"""
    load_entity_dictionary(entities=["项目B"])
    got = _names("张三决定项目B延期")
    assert not any(
        name == "项目B" and kind == "person" for name, kind in got
    ), f"「项目B」是 topic，不该是人。实际: {got}"


# ---------------- 过滤 ----------------

def test_dirty_names_are_rejected() -> None:
    """粘连产物不能被灌进词典，否则错误会被固化。

    `和小王` 会被剥成 `小王` 再灌 —— 剥掉连词是对的，别把连词也灌进去。
    """
    from memory.services.labeler import _DICT_LOADED, reset_dictionary

    load_entity_dictionary(names=["和小王"])
    assert "和小王" not in _DICT_LOADED, "连词不该留在词条里"
    assert "小王" in _DICT_LOADED, "剥掉连词后应该收下"
    reset_dictionary()


def test_single_char_rejected() -> None:
    from memory.services.labeler import _DICT_LOADED

    load_entity_dictionary(names=["是"])
    assert "是" not in _DICT_LOADED, "单字不该收"


def test_name_containing_stop_char_still_accepted() -> None:
    """「明」在 `_STOP_CHARS` 里（来自「证明」），但「小明」是个正经名字。

    这里**不能**用 `_has_stop_char` 筛 —— 那个函数是给「截断的句子片段」
    用的，图上的名字已经过一轮抽取了。用它会误伤一大批名字。
    """
    from memory.services.labeler import _DICT_LOADED

    load_entity_dictionary(names=["小明"])
    assert "小明" in _DICT_LOADED, "含停用字的合法名字不该被拒"


def test_stop_entities_rejected() -> None:
    from memory.services.labeler import _DICT_LOADED

    load_entity_dictionary(names=["这个项目", "项目"])
    assert "这个项目" not in _DICT_LOADED


def test_load_is_idempotent() -> None:
    """重复灌同一个名字，第二次返回 0。"""
    first = load_entity_dictionary(names=["豆包"])
    second = load_entity_dictionary(names=["豆包"])
    assert first == 1
    assert second == 0


def test_empty_input_is_safe() -> None:
    assert load_entity_dictionary() == 0
    assert load_entity_dictionary(names=[]) == 0
