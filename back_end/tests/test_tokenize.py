"""检索分词单测。

重点是**中文短查询能命中**——这正是 M3 阶段 BM25 通道失效的根因。
"""

from __future__ import annotations

import pytest

from memory.core.tokenize import (
    is_cjk,
    tokenize,
    tokenize_for_index,
    tokenize_for_query,
)


# ---------------- 基本切分 ----------------


def test_pure_chinese_splits_to_bigrams() -> None:
    toks = tokenize("张三决定")
    assert toks == ["张三", "三决", "决定"]


def test_single_char_survives() -> None:
    """单字不能被 bigram 逻辑丢掉，否则「好」这种查询永远不命中。"""
    assert tokenize("好") == ["好"]
    assert tokenize("很 好") == ["很", "好"]


def test_two_char_word_is_a_token() -> None:
    """2 字查询能命中，这是整个改造的核心目标。"""
    toks = tokenize("张三决定项目A延期到六月")
    assert "延期" in toks


def test_three_char_query_is_covered() -> None:
    """3 字查询拆成多个 bigram，全部都在索引里。"""
    toks = tokenize("张三决定项目A延期到六月")
    assert "项目" in toks
    assert "延期" in toks


def test_cross_script_bigram_not_produced() -> None:
    """CJK 与拉丁字符分属不同 run，不产出跨界 bigram（`目A` 这种）。

    这是刻意的：查询 `项目A` 同样被切成 `项目` + `a`，两侧规则一致就能匹配，
    而索引里少一堆跨界组合更省空间。
    """
    toks = tokenize("项目A延期")
    assert "项目" in toks
    assert "a" in toks          # 拉丁段整体保留（已小写）
    assert "目a" not in toks    # 跨界不产出
    assert "延期" in toks


def test_latin_words_lowercased() -> None:
    assert tokenize("Project Alpha") == ["project", "alpha"]


def test_mixed_script() -> None:
    toks = tokenize("项目A延期")
    assert "项目" in toks
    assert "a" in toks


def test_digits_preserved() -> None:
    assert tokenize("2026") == ["2026"]


def test_punctuation_dropped() -> None:
    toks = tokenize("延期，到六月。")
    assert "，" not in "".join(toks)
    assert "延期" in toks
    assert "到六" in toks


def test_empty_input() -> None:
    assert tokenize("") == []
    assert tokenize_for_index("") == ""
    assert tokenize_for_query("") == ""


def test_whitespace_only() -> None:
    assert tokenize("   ") == []


# ---------------- 边界 ----------------


def test_long_chinese_run() -> None:
    text = "一二三四五六七八九十"
    toks = tokenize(text)
    # n 个字符 → n-1 个 bigram
    assert len(toks) == len(text) - 1
    assert toks[0] == "一二"
    assert toks[-1] == "九十"


def test_cjk_detection_ranges() -> None:
    assert is_cjk("中")
    assert is_cjk("あ")   # 平假名
    assert is_cjk("カ")   # 片假名
    assert is_cjk("한")   # 谚文
    assert not is_cjk("a")
    assert not is_cjk("1")
    assert not is_cjk("，")


def test_emoji_does_not_crash() -> None:
    toks = tokenize("延期🎉到六月")
    assert "延期" in toks


# ---------------- 索引 / 查询两侧一致 ----------------


def test_index_and_query_use_same_rules() -> None:
    """两侧规则必须一致，否则 BM25 对不上。"""
    text = "张三决定项目A延期到六月"
    assert tokenize_for_index(text) == tokenize_for_query(text)


def test_query_token_appears_in_index_tokens() -> None:
    """端到端契约：2 字查询的 token 必须能在索引 token 串里找到。"""
    index = tokenize_for_index("张三决定项目A延期到六月").split()
    for query in ["延期", "项目", "六月", "决定"]:
        q_toks = tokenize_for_query(query).split()
        assert all(t in index for t in q_toks), f"{query} 的 token 没进索引"


def test_joined_with_spaces() -> None:
    """必须是空格连接——Weaviate 的 word tokenizer 靠空格切。"""
    idx = tokenize_for_index("张三决定")
    assert idx == "张三 三决 决定"
    assert "  " not in idx


# ---------------- 回归：M3 阶段的病 ----------------


@pytest.mark.parametrize(
    "query",
    ["延期", "项目", "启动", "时间", "会议", "预算"],
)
def test_two_char_queries_regression(query: str) -> None:
    """M3 阶段这些 2 字查询在 BM25 全是 0 命中。

    bigram 之后，只要原文含这个词，它的 bigram 就一定在索引里。
    """
    corpus = "张三决定项目A延期到六月李四确定新项目B启动时间会议预算讨论"
    assert query in corpus  # 前提：词确实在语料里
    assert query in tokenize(corpus)
