"""B7 `event_id` 生成 —— 单测。

覆盖点：
1. 同内容 → 同 id（幂等地基）
2. namespace 参与哈希（跨域不撞）
3. 字段顺序无关（types / entities 排序）
4. normalize 的等价性（空白、大小写、全角、标点）
5. 边界变化 → id 变化（切分边界不同就是不同事件）
"""

from __future__ import annotations

from datetime import date

import pytest

from memory.core.event_id import build_canonical, make_event_id, normalize_summary

BASE = dict(
    namespace="work",
    date_start=date(2026, 3, 1),
    date_end=None,
    types=["work", "decision"],
    entity_names=["张三", "项目A"],
    summary="张三决定项目A延期到六月",
)


def test_same_input_same_id() -> None:
    """同内容重复写入必须得到同一 id，否则幂等失效（A7）。"""
    a = make_event_id(**BASE)
    b = make_event_id(**BASE)
    assert a == b
    assert a.startswith("evt_") and len(a) == 4 + 16


def test_namespace_participates_in_hash() -> None:
    """两个 namespace 记同一句话不能撞 id（ADR D1 硬要求）。"""
    work = make_event_id(**BASE)
    personal = make_event_id(**{**BASE, "namespace": "personal"})
    assert work != personal


def test_types_order_irrelevant() -> None:
    a = make_event_id(**{**BASE, "types": ["work", "decision"]})
    b = make_event_id(**{**BASE, "types": ["decision", "work"]})
    assert a == b


def test_entity_order_irrelevant() -> None:
    a = make_event_id(**{**BASE, "entity_names": ["张三", "项目A"]})
    b = make_event_id(**{**BASE, "entity_names": ["项目A", "张三"]})
    assert a == b


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("记住了", "  记住了  "),
        ("Remember This", "remember this"),
        ("记住了！", "记住了"),
        ("ＡＢＣ", "abc"),
        ("，。项目A！", "项目a"),
    ],
)
def test_normalize_equivalence(left: str, right: str) -> None:
    """空白/大小写/全角/标点的差异不应产生不同 id。"""
    assert normalize_summary(left) == normalize_summary(right)


def test_date_change_changes_id() -> None:
    """日期是事件身份的一部分：换一天就是另一件事。"""
    a = make_event_id(**BASE)
    b = make_event_id(**{**BASE, "date_start": date(2026, 3, 2)})
    assert a != b


def test_date_end_null_vs_value() -> None:
    """持续中（null）与有结束日必须区分开。"""
    a = make_event_id(**BASE)
    b = make_event_id(**{**BASE, "date_end": date(2026, 6, 1)})
    assert a != b


def test_summary_change_changes_id() -> None:
    a = make_event_id(**BASE)
    b = make_event_id(**{**BASE, "summary": "张三决定项目A按期上线"})
    assert a != b


def test_canonical_shape() -> None:
    """canonical 的六段结构必须与 ADR 一致，排障时才对得上。"""
    canonical = build_canonical(**BASE)
    parts = canonical.split("|")
    assert parts == [
        "work",
        "2026-03-01",
        "",
        "decision,work",
        "张三,项目A",
        "张三决定项目a延期到六月",
    ]
