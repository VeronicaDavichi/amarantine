"""spec 039 · 动词映射表（2026-11-18 按人工标注补）。"""

from __future__ import annotations

import pytest

from memory.services import relations as R


def test_collab_verbs_mapped_from_labels() -> None:
    """`讨论`/`确认`/`同意`/`去` 已按行为组人工标注补进表。

    依据：10 条行为标注里 `讨论` 6 次（判 2/1/1/0/0/0）、`去` 4 次（判 2/1/0/0），
    两个都被判过「2 分＝有关系·协作」。
    """
    for verb in ("讨论", "确认", "同意", "去"):
        assert R.predicate_for_verb(verb) == "COLLABORATES_WITH", verb


def test_maintain_verbs_unchanged() -> None:
    for verb in ("部署", "发布", "更新", "重启", "回滚", "维护", "对接", "上架"):
        assert R.predicate_for_verb(verb) == "MAINTAINS", verb


def test_owns_verbs_unchanged() -> None:
    for verb in ("修改", "修复", "实现", "新增"):
        assert R.predicate_for_verb(verb) == "OWNS", verb


def test_unknown_verb_returns_none() -> None:
    """没进表的动词不建边 —— 不猜。"""
    for verb in ("吃", "睡", "笑", "看", "", None):
        assert R.predicate_for_verb(verb) is None


def test_leaked_verb_not_mapped() -> None:
    """`学习`/`批评` 只有 1 次证据，暂不映射（宁缺勿滥）。"""
    assert R.predicate_for_verb("学习") is None
    assert R.predicate_for_verb("批评") is None


def test_all_mapped_predicates_in_predicate_specs() -> None:
    """映射目标必须是已定义的谓词，否则写边时会被跳过。"""
    for verb, pred in R.VERB_TO_PREDICATE.items():
        assert pred in R.PREDICATE_SPECS, f"{verb} → {pred} 不在谓词表里"
