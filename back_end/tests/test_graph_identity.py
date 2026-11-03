"""一个 key 在图上只能有一个节点。

`MERGE` 一旦带标签，同一个 key 就能存在两次：别名声明建 `:Entity`、
实体抽取建 `:Person`，两边各匹配各的，永远合不到一起。

这组测试锁的是**cypher 文本本身** —— FakeDriver 不执行它，所以只能
按字符串检查。真库行为在 `verify_key_merge.py` 跑过（见提交说明）。
"""

from __future__ import annotations

import re

from memory.services import graph_outbox as GO


def _merge_clauses(cypher: str) -> list[str]:
    r"""抽出所有 MERGE 子句，**包括关系型**。

    原来的 `MERGE\s*\([^)]*\)` 只能匹配节点模式，遇到
    `MERGE (p)-[r:ALIAS_OF]->(a)` 会截成 `MERGE (p)` ——
    调用方无法区分「节点按 p 合并」和「合并这条关系」。
    """
    return re.findall(
        r"MERGE\s*\([^)]*\)(?:\s*-\s*\[[^\]]*\]\s*->\s*\([^)]*\))?",
        cypher,
    )


# ---------------- 不带标签合并 ----------------

def test_person_merge_has_no_label() -> None:
    """`MERGE (p:Person {key})` 是最初的写法，也是双节点的根因。"""
    clauses = _merge_clauses(GO.APPEND_PERSON_CYPHER)
    assert clauses, "应该有一条 MERGE"
    for c in clauses:
        assert ":Person" not in c, f"人和实体的合并不能带标签: {c}"
        assert ":Entity" not in c, f"人和实体的合并不能带标签: {c}"


def test_entity_merge_has_no_label() -> None:
    clauses = _merge_clauses(GO.APPEND_ENTITY_CYPHER)
    assert clauses
    for c in clauses:
        assert ":Entity" not in c, f"合并不能带标签: {c}"


def test_alias_host_merge_has_no_person_label() -> None:
    """`ALIAS_OF` 的宿主不该被硬绑成 Person ——「豆包」是个产品。"""
    clauses = _merge_clauses(GO.APPEND_ALIAS_CYPHER)
    host = [c for c in clauses if "item.key" in c]
    assert host, "宿主那条 MERGE 应该按 key"
    for c in host:
        assert ":Person" not in c, f"别名宿主不能硬绑 Person: {c}"
        assert ":Entity" not in c, f"别名宿主不能硬绑 Entity: {c}"


# ---------------- 标签在创建时打，但人能补 ----------------

def test_person_label_applied_unconditionally() -> None:
    """`SET p:Person` 不带 ON CREATE。

    别名先把「豆包」建成 `:Entity`，实体抽取判定它是人之后要能**补上**
    `:Person`，而不是另建节点。`ON CREATE SET p:Person` 会让标签丢掉。
    """
    cypher = GO.APPEND_PERSON_CYPHER
    assert "SET p:Person" in cypher
    assert "ON CREATE SET p:Person" not in cypher


def test_entity_label_applied_unconditionally() -> None:
    cypher = GO.APPEND_ENTITY_CYPHER
    assert "SET n:Entity" in cypher
    assert "ON CREATE SET n:Entity" not in cypher


def test_name_only_set_on_create() -> None:
    """名字只在新建时写 —— 重放不能把用户改过的名字顶回去。

    检查方式：把每一行的 `ON CREATE SET` 前缀剥掉之后，不该还剩
    `.name =` 之类的赋值。直接找 `SET` 子句会误命中 `ON CREATE SET`
    里的 `SET`，所以按行处理。
    """
    for cypher in (
        GO.APPEND_PERSON_CYPHER,
        GO.APPEND_ENTITY_CYPHER,
        GO.APPEND_ALIAS_CYPHER,
    ):
        for line in cypher.splitlines():
            stripped = line.strip()
            if not stripped.startswith("SET "):
                continue
            assert ".name" not in stripped, (
                f"name 不能无条件 SET，会顶掉用户改过的名字: {stripped}"
            )
            assert ".kind" not in stripped, (
                f"kind 不能无条件 SET: {stripped}"
            )


# ---------------- 别名反查不限定标签 ----------------

def test_resolve_alias_does_not_require_person() -> None:
    """反查时起点若写死 `(p:Person)`，宿主是 Entity 的别名就查不出来。"""
    cypher = GO.RESOLVE_ALIAS_CYPHER
    assert ":Person" not in cypher, f"反查不能限定 Person: {cypher}"


# ---------------- key 参与合并 ----------------

def test_all_merges_use_key_not_name() -> None:
    """身份是 key。按 name 合并会让改名撞名无法表达。"""
    for cypher, label in (
        (GO.APPEND_PERSON_CYPHER, "person"),
        (GO.APPEND_ENTITY_CYPHER, "entity"),
        (GO.APPEND_ALIAS_CYPHER, "alias"),
    ):
        for c in _merge_clauses(cypher):
            if "Alias" in c:
                continue  # (:Alias) 按 name 建，它本身就是名字容器
            if "-[" in c:
                # 合并的是**关系**（`MERGE (p)-[r:ALIAS_OF]->(a)`），
                # 端点由前面的节点 MERGE 定好了身份，这里不涉及 key。
                # 它的作用是让一对 (canonical, alias) 只保留一条边 ——
                # 重复补建边时不会多出平行边。
                continue
            assert "item.key" in c or "$key" in c, (
                f"{label} 的 MERGE 必须按 key: {c}"
            )
