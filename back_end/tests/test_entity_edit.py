"""实体纠错：改 / 拆 / 合 / 解挂。

这里测的是**语义边界**，不是 cypher 本身 ——
真库行为已经在 `verify_entity_edit.py` 里跑过了。
"""

from __future__ import annotations

import pytest

from memory.config import Settings
from memory.core.entity_key import fresh_key, is_key, key_for
from memory.services.entity_edit import (
    EditResult,
    merge_entities,
    rebind_event_entity,
    rename_entity,
    split_entity,
)


def _settings() -> Settings:
    return Settings(
        MEMORY_NAMESPACE="work",
        WEAVIATE_URL="http://weaviate.test",
        NEO4J_URI="bolt://neo4j.test:7687",
    )


class FakeRow:
    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, k):
        return self._data[k]

    def get(self, k, default=None):
        return self._data.get(k, default)


class FakeResult:
    """按脚本返回若干行。最后一行会被 `.single()` 用。"""

    def __init__(self, rows: list[dict] | dict | None = None) -> None:
        if rows is None:
            self.rows: list[dict] = []
        elif isinstance(rows, dict):
            self.rows = [rows]
        else:
            self.rows = rows

    def __iter__(self):
        return iter([FakeRow(r) for r in self.rows])

    def single(self):
        return FakeRow(self.rows[0]) if self.rows else None

    def consume(self):
        return self


class FakeSession:
    def __init__(self, driver: "FakeDriver") -> None:
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cypher: str, **params):
        self.driver.calls.append((cypher, params))
        return self.driver.respond(cypher, params)


class FakeDriver:
    """按 cypher 特征返回预设结果。"""

    def __init__(self, by_name: dict[str, list[dict]] | None = None) -> None:
        self.by_name = by_name or {}
        self.calls: list[tuple[str, dict]] = []
        self.session_obj = FakeSession(self)

    def session(self):
        return self.session_obj

    def respond(self, cypher: str, params: dict) -> FakeResult:
        """按 **cypher 内容特征**匹配，不按常量名。

        `drv.calls` 里存的是真正发出去的 cypher 文本，里面没有
        `MARK_SPLIT_CYPHER` 这种 Python 常量名。靠常量名匹配会永远不命中，
        测试就变成「假装通过」了。
        """
        # 留痕要最先判：它跟别的语句没有共同片段
        if "CREATE (ed:EntityEdit" in cypher:
            return FakeResult({"ed.created_at": "2026-11-02T00:00:00+00:00"})
        if "RETURN n.key AS key, n.name AS name, labels(n) AS labels" in cypher:
            return FakeResult(self.by_name.get(params.get("name"), []))
        if "SET n.name = $to" in cypher:
            return FakeResult({"renamed": 1})
        if "SET n.split_into" in cypher:
            return FakeResult({"marked": 1})
        if "orphan_since" in cypher:
            return FakeResult({"name": params.get("name"), "refs": 0})
        if "from_split: true" in cypher:
            return FakeResult({"count(*)": 1})
        if "MERGE (c:Entity" in cypher:
            return FakeResult({"key": params["key"], "name": params["name"]})
        if "REMOVE src:Person" in cypher:
            return FakeResult({"alias": "x", "canonical": "y"})
        if "]->(dst)" in cypher and "INVOLVES" in cypher:
            return FakeResult({"count(*)": 2})
        if "]->(dst)" in cypher and "MENTIONS" in cypher:
            return FakeResult({"count(*)": 3})
        if "from_rebind: true" in cypher:
            return FakeResult({"key": "e_new", "name": params.get("to_name")})
        if "DELETE r" in cypher:
            return FakeResult({"count(*)": 1})
        if "RETURN count(*) AS c" in cypher:
            return FakeResult({"c": 1})
        return FakeResult()


def _driver(**names) -> FakeDriver:
    """names: 名字 -> 节点列表"""
    return FakeDriver({k: v for k, v in names.items()})


def _node(name: str, key: str, label: str = "Person") -> dict:
    return {"key": key, "name": name, "labels": [label]}


# ---------------- key ----------------

def test_same_name_same_key() -> None:
    """同名默认同一个实体 —— 否则每句话都长出节点。"""
    assert key_for("豆包") == key_for("豆包")


def test_different_name_different_key() -> None:
    assert key_for("豆包") != key_for("通义千问")


def test_fresh_key_differs_each_time() -> None:
    """重名时另起代号，每次都不一样。"""
    assert fresh_key("豆包") != fresh_key("豆包")


def test_is_key_recognises_generated() -> None:
    assert is_key(key_for("豆包"))
    assert not is_key("豆包")
    assert not is_key(None)
    assert not is_key("e_short")


# ---------------- 改 ----------------

def test_rename_succeeds_unconditionally() -> None:
    """**改名永不冲突** —— key 是身份，name 只是显示名。"""
    drv = _driver(助手=[_node("助手", "k1")])
    r = rename_entity("work", "助手", "豆包", settings=_settings(), driver=drv)
    assert r.ok, r.error
    assert r.detail["to"] == "豆包"


def test_rename_to_self_rejected() -> None:
    drv = _driver(豆包=[_node("豆包", "k1")])
    r = rename_entity("work", "豆包", "豆包", settings=_settings(), driver=drv)
    assert not r.ok
    assert "相同" in r.error


def test_rename_missing_entity_rejected() -> None:
    drv = _driver()
    r = rename_entity("work", "不存在", "新名", settings=_settings(), driver=drv)
    assert not r.ok
    assert "没有叫" in r.error


def test_rename_empty_rejected() -> None:
    r = rename_entity("work", "", "x", settings=_settings(), driver=_driver())
    assert not r.ok


def test_rename_does_not_merge_when_target_exists() -> None:
    """目标已存在时，改名**不碰**目标节点。

    改的是显示名，不是身份。两个豆包各自保留 key。
    """
    drv = _driver(助手=[_node("助手", "k1")], 豆包=[_node("豆包", "k2")])
    r = rename_entity("work", "助手", "豆包", settings=_settings(), driver=drv)
    assert r.ok
    # 不该有任何搬边的动作
    assert not any("]->(dst)" in c for c, _ in drv.calls)


# ---------------- 合 ----------------

def test_merge_moves_edges_and_demotes() -> None:
    drv = _driver(小张=[_node("小张", "k1")], 张三=[_node("张三", "k2")])
    r = merge_entities("work", "小张", "张三", settings=_settings(), driver=drv)
    assert r.ok, r.error
    assert r.detail["moved"] == {"involves": 2, "mentions": 3}
    ops = [c for c, _ in drv.calls]
    assert any("REMOVE src:Person" in c for c in ops), "被并的应降级成 Alias"


def test_merge_self_rejected() -> None:
    drv = _driver(张三=[_node("张三", "k2")])
    r = merge_entities("work", "张三", "张三", settings=_settings(), driver=drv)
    assert not r.ok


def test_merge_missing_target_rejected() -> None:
    drv = _driver(小张=[_node("小张", "k1")])
    r = merge_entities("work", "小张", "张三", settings=_settings(), driver=drv)
    assert not r.ok
    assert "张三" in r.error


def test_merge_never_deletes() -> None:
    """全程不该出现 DETACH DELETE —— 纠错不是抹除痕迹。"""
    drv = _driver(小张=[_node("小张", "k1")], 张三=[_node("张三", "k2")])
    merge_entities("work", "小张", "张三", settings=_settings(), driver=drv)
    assert not any("DETACH DELETE" in c for c, _ in drv.calls)


# ---------------- 拆 ----------------

def test_split_requires_two_children() -> None:
    r = split_entity("work", "张三李四", ["张三"], settings=_settings(), driver=_driver())
    assert not r.ok
    assert "两个" in r.error


def test_split_rejects_target_containing_source() -> None:
    r = split_entity(
        "work", "张三", ["张三", "李四"], settings=_settings(), driver=_driver()
    )
    assert not r.ok


def test_split_marks_original_without_deleting() -> None:
    drv = _driver(张三李四=[_node("张三李四", "k1", "Entity")])
    r = split_entity(
        "work", "张三李四", ["张三", "李四"], settings=_settings(), driver=drv
    )
    assert r.ok, r.error
    assert [c["name"] for c in r.detail["children"]] == ["张三", "李四"]
    assert any("SET n.split_into" in c for c, _ in drv.calls)
    assert not any("DETACH DELETE" in c for c, _ in drv.calls)


def test_split_links_children_with_flag() -> None:
    """子实体的边标 `from_split=true` —— 这不是精确归属。"""
    drv = _driver(张三李四=[_node("张三李四", "k1", "Entity")])
    split_entity("work", "张三李四", ["张三", "李四"], settings=_settings(), driver=drv)
    link_calls = [c for c, _ in drv.calls if "from_split: true" in c]
    assert link_calls
    assert all("from_split: true" in c for c in link_calls)


# ---------------- 解挂 ----------------

def test_rebind_unbinds_then_rebinds() -> None:
    drv = _driver(三哥=[_node("三哥", "k1")])
    r = rebind_event_entity(
        "work", "evt_a", "三哥", "印度人", settings=_settings(), driver=drv
    )
    assert r.ok, r.error
    ops = [c for c, _ in drv.calls]
    assert any("DELETE r" in c for c in ops), "应先解挂"
    assert any("from_rebind" in c for c in ops), "再重挂"


def test_rebind_requires_event_mentions_old() -> None:
    """这条事件本来就没提到它，凭什么解挂。"""
    drv = FakeDriver()

    def respond(cypher: str, params: dict):
        if "RETURN count(*) AS c" in cypher:
            return FakeResult({"c": 0})
        return FakeDriver.respond(drv, cypher, params)

    drv.respond = respond  # type: ignore[method-assign]
    r = rebind_event_entity(
        "work", "evt_a", "三哥", "印度人", settings=_settings(), driver=drv
    )
    assert not r.ok
    assert "没有提到" in r.error


def test_rebind_same_target_rejected() -> None:
    r = rebind_event_entity(
        "work", "evt_a", "三哥", "三哥", settings=_settings(), driver=_driver()
    )
    assert not r.ok


def test_rebind_marks_orphan_but_does_not_delete() -> None:
    """旧节点没人引用了也**不删** —— 别的句子可能还要用。"""
    drv = _driver(三哥=[_node("三哥", "k1")])
    rebind_event_entity(
        "work", "evt_a", "三哥", "印度人", settings=_settings(), driver=drv
    )
    assert any("orphan_since" in c for c, _ in drv.calls)
    assert not any("DETACH DELETE" in c for c, _ in drv.calls)


def test_rebind_to_person_uses_involves() -> None:
    """`to_kind=person` 走 INVOLVES，不是 MENTIONS —— 系统不猜目标是什么。"""
    drv = _driver(三哥=[_node("三哥", "k1")])
    r = rebind_event_entity(
        "work",
        "evt_a",
        "三哥",
        "老王",
        settings=_settings(),
        to_kind="person",
        driver=drv,
    )
    assert r.ok
    rebinds = [c for c, _ in drv.calls if "from_rebind: true" in c]
    assert rebinds, "应发出重挂语句"
    assert all("INVOLVES" in c for c in rebinds), "to_kind=person 应走 INVOLVES"


# ---------------- 留痕 ----------------

@pytest.mark.parametrize("op", ["rename", "merge", "split", "rebind"])
def test_every_op_records_edit(op: str) -> None:
    """四种操作都要留痕：谁、何时、做了什么、为什么。"""
    if op == "rename":
        drv = _driver(助手=[_node("助手", "k1")])
        rename_entity("work", "助手", "豆包", settings=_settings(), reason="r", driver=drv)
    elif op == "merge":
        drv = _driver(小张=[_node("小张", "k1")], 张三=[_node("张三", "k2")])
        merge_entities("work", "小张", "张三", settings=_settings(), reason="r", driver=drv)
    elif op == "split":
        drv = _driver(张三李四=[_node("张三李四", "k1", "Entity")])
        split_entity(
            "work", "张三李四", ["张三", "李四"], settings=_settings(), reason="r", driver=drv
        )
    else:
        drv = _driver(三哥=[_node("三哥", "k1")])
        rebind_event_entity(
            "work", "evt_a", "三哥", "印度人", settings=_settings(), reason="r", driver=drv
        )

    records = [(c, p) for c, p in drv.calls if "EntityEdit" in c]
    assert records, f"{op} 没有留痕"
    _, params = records[0]
    assert params["op"] == op
    assert params["reason"] == "r"
    assert isinstance(params["args"], str), "args 必须序列化成 JSON 字符串"


def test_edit_result_to_dict() -> None:
    r = EditResult(True, "rename", detail={"from": "a", "to": "b"})
    d = r.to_dict()
    assert d["ok"] is True and d["op"] == "rename"
