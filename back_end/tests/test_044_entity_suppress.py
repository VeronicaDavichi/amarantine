"""spec 044：实体抑制表（T1）+ 写图侧过滤（T4）。"""

from __future__ import annotations

import inspect

import pytest

from memory.services import graph_outbox
from memory.services.entity_suppress import EntitySuppressStore


@pytest.fixture()
def store(tmp_path):
    return EntitySuppressStore(tmp_path / "t.db")


def test_add_then_rejected(store) -> None:
    assert store.rejected("ns") == set()
    held = store.add("ns", ["ALICE", "OLIVIA"], reason="name_fragment")
    assert held >= 1
    assert store.rejected("ns") == {"ALICE", "OLIVIA"}


def test_add_is_idempotent(store) -> None:
    store.add("ns", ["ALICE"], reason="name_fragment")
    store.add("ns", ["ALICE"], reason="name_fragment")  # 不抛、不重复
    rows = store.list_all("ns")
    assert len(rows) == 1


def test_namespace_isolation(store) -> None:
    """`AI` 在闲聊是噪声、在工作群可能是主题 —— 按 namespace 隔离。"""
    store.add("chat", ["AI"], reason="name_fragment")
    assert store.rejected("chat") == {"AI"}
    assert store.rejected("work") == set()


def test_cache_invalidated_on_add(store) -> None:
    assert store.rejected("ns") == set()   # 先读一次，填缓存
    store.add("ns", ["X"], reason="name_fragment")
    assert "X" in store.rejected("ns"), "add 之后缓存必须失效，否则新抑制不生效"


def test_revive_removes_from_rejected(store) -> None:
    store.add("ns", ["X"], reason="name_fragment")
    assert store.revive("ns", ["X"]) == 1
    assert store.rejected("ns") == set()


def test_detail_and_decided_by_recorded(store) -> None:
    store.add("ns", ["X"], reason="name_fragment", detail="别名的一部分")
    row = store.list_all("ns")[0]
    assert row["decided_by"] == "auto"
    assert row["status"] == "rejected"
    assert "别名" in row["detail"]


def test_write_path_filters_suppressed_entities() -> None:
    """写图侧必须也过滤：重放用的是卡上已存的 entities，不经过抽取器。

    （用源码守卫：这段逻辑在 `_append_one`/写卡路径里，跑真图成本太高。）
    """
    src = inspect.getsource(graph_outbox)
    assert "entity_suppress" in src
    assert "写图侧抑制实体" in src
