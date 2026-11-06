"""spec 030：hub 保护的**端到端**验证。

## 这个文件补的是哪个洞

现有测试是这样的：

| 层 | 有没有测 | 怎么测的 |
|---|---|---|
| `forget_policy` 拿到 degree=8 会保护 | ✅ | 直接构造 `ForgetCandidate(graph_degree=8)` |
| `event_degrees` 解析 driver 返回 | ✅ | 注入 fake driver，喂假的 records |
| **`event_degrees` 的 Cypher 在真图上** | ❌ | **这里补** |
| **造图 → 算度 → 保护 的整条链** | ❌ | **这里补** |

fake driver 测的是「解析逻辑」，不是「Cypher 写得对不对」。
如果 `_DEGREE_CYPHER` 漏了某种边、数错方向、或者 `namespace` 过滤写错，
上面那几条会**全部通过** —— 这就是「未验证分支」的真实含义：
零件都合格，装起来能不能跑没人知道。

## 为什么不复用真实 namespace

必须用**临时 namespace + finally 清理**（spec 027 的教训：
造数据的脚本不在 finally 里清，中断一次就留一堆垃圾 —— 那次清了 32 个）。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from memory.config import load_settings
from memory.core.forget_policy import (
    BUCKET_PROTECTED,
    ForgetCandidate,
    classify,
)
from memory.services.graph_search import event_degrees

OLD = date(2024, 1, 1)


def _neo4j_or_skip():
    """Neo4j 连不上就 skip。

    **注意**：如果 CI 永远连不上，这个文件就永远 skip —— 等于没测。
    所以验收标准写明「本地必须有 Neo4j，要看到 passed 不是 skipped」。
    """
    neo4j = pytest.importorskip("neo4j", reason="没装 neo4j 驱动")
    s = load_settings()
    try:
        drv = neo4j.GraphDatabase.driver(
            s.NEO4J_URI, auth=(s.NEO4J_USER, s.NEO4J_PASSWORD), connection_timeout=3.0
        )
        drv.verify_connectivity()
        return s, drv
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Neo4j 不可用: {exc}")


class TempNamespace:
    """在 Neo4j 里造一个用完即删的 namespace。"""

    def __init__(self, driver, prefix: str = "hubtest") -> None:
        self.driver = driver
        self.ns = f"{prefix}_{uuid.uuid4().hex[:8]}"
        self.created: list[str] = []

    def make_hub_event(self, event_id: str, degree: int) -> None:
        """造一个 Event，连 degree 个邻居（每个邻居一条边）。

        degree=8 → 8 个邻居 8 条边 → `size([(e)--()|1])` 应为 8。
        """
        with self.driver.session() as sess:
            sess.run(
                """
                MERGE (e:Event {event_id: $eid, namespace: $ns})
                SET e.summary = $summary
                RETURN e
                """,
                eid=event_id,
                ns=self.ns,
                summary=f"测试 hub 事件 degree={degree}",
            )
            # 邻居用不同 label，模拟真实的 Time / Action / Entity / Person
            labels = ["Time", "Action", "Entity", "Person"]
            for i in range(degree):
                lb = labels[i % len(labels)]
                sess.run(
                    f"""
                    MATCH (e:Event {{event_id: $eid, namespace: $ns}})
                    MERGE (n:`{lb}` {{uid: $uid, namespace: $ns}})
                    MERGE (e)-[:TESTLINK]->(n)
                    """,
                    eid=event_id,
                    ns=self.ns,
                    uid=f"{self.ns}_n{i}",
                )
        self.created.append(event_id)

    def cleanup(self) -> int:
        """删掉这个 namespace 下的一切，返回删除的节点数。"""
        with self.driver.session() as sess:
            r = sess.run(
                "MATCH (n) WHERE n.namespace = $ns "
                "WITH n LIMIT 10000 "
                "DETACH DELETE n "
                "RETURN count(*) AS deleted",
                ns=self.ns,
            ).data()
            return r[0]["deleted"] if r else 0

    def count(self) -> int:
        with self.driver.session() as sess:
            r = sess.run(
                "MATCH (n) WHERE n.namespace = $ns RETURN count(n) AS n", ns=self.ns
            ).data()
            return r[0]["n"] if r else 0


@pytest.fixture()
def temp_ns():
    s, drv = _neo4j_or_skip()
    t = TempNamespace(drv)
    try:
        yield s, t
    finally:
        # **必须 finally**：测试中途抛异常也要清干净。
        # 清理后立刻验证，清不干净就直接报错，不留残余。
        t.cleanup()
        left = t.count()
        drv.close()
        assert left == 0, (
            f"临时 namespace {t.ns} 还有 {left} 个节点没清掉 —— "
            "下一次跑会污染图统计"
        )


# ============================================================ 核心：Cypher 算得对


def test_event_degrees_counts_real_edges(temp_ns) -> None:
    """在真图上造 8 条边，`event_degrees` 必须返回 8。

    **这是本 spec 的核心**：之前所有 `event_degrees` 测试都是 fake driver，
    验的是「解析」；这条验的是「Cypher 到底数了几个东西」。
    """
    s, t = temp_ns
    t.make_hub_event("evt_hub8", 8)

    out = event_degrees(["evt_hub8"], t.ns, settings=s)

    assert out is not None, "event_degrees 返回 None 表示图不可用，不该在真图上发生"
    assert out.get("evt_hub8") == 8, (
        f"造了 8 条边，算出来是 {out.get('evt_hub8')} —— Cypher 数错了"
    )


def test_event_degrees_counts_both_directions(temp_ns) -> None:
    """边方向不影响度数（`(e)--()` 无方向）。

    如果哪天有人把 Cypher 改成 `(e)-->(n)`，反向的边就漏了，
    度数会偏小 —— hub 保护会漏判。
    """
    s, t = temp_ns
    t.make_hub_event("evt_hub8", 8)
    with t.driver.session() as sess:
        # 再加三条**指向** Event 的边（入边）
        for i in range(3):
            sess.run(
                """
                MATCH (e:Event {event_id: $eid, namespace: $ns})
                MERGE (n:Action {uid: $uid, namespace: $ns})
                MERGE (n)-[:TESTIN]->(e)
                """,
                eid="evt_hub8",
                ns=t.ns,
                uid=f"{t.ns}_in{i}",
            )

    out = event_degrees(["evt_hub8"], t.ns, settings=s)
    assert out is not None
    assert out.get("evt_hub8") == 11, (
        f"8 出边 + 3 入边 = 11，算出来是 {out.get('evt_hub8')} —— "
        "Cypher 只数了一个方向"
    )


def test_event_degrees_is_namespace_scoped(temp_ns) -> None:
    """别的 namespace 里的节点不能被算进来。

    **第一版设计是错的**：在另一个 namespace 造**同名** event_id 的节点，
    然后断言查到的度数 == 8。问题是不加 namespace 过滤时 Cypher 会返回
    **两行同一个 event_id**，字典推导式后一行覆盖前一行 ——
    结果取决于 Neo4j 的遍历顺序，测试时过时不过。
    meta-test 注入「去掉 namespace 过滤」时它照样通过，暴露了这个缺陷。

    现在改成**不同 event_id**：查一个只存在于别的 namespace 的 id，
    过滤生效时它根本不该出现在返回字典里。判定是干净的「有 / 没有」。
    """
    s, t = temp_ns
    t.make_hub_event("evt_hub8", 8)

    other_ns = f"{t.ns}_other"
    with t.driver.session() as sess:
        try:
            sess.run(
                """
                MERGE (e:Event {event_id: $eid, namespace: $ns})
                SET e.summary = '污染源'
                """,
                eid="evt_only_in_other",
                ns=other_ns,
            )
            for i in range(5):
                sess.run(
                    """
                    MATCH (e:Event {event_id: $eid, namespace: $ns})
                    MERGE (n:Time {uid: $uid, namespace: $ns})
                    MERGE (e)-[:TESTPOLLUTE]->(n)
                    """,
                    eid="evt_only_in_other",
                    ns=other_ns,
                    uid=f"{other_ns}_{i}",
                )

            # 只查别的 namespace 里那个 id，限定在 t.ns
            out = event_degrees(["evt_only_in_other"], t.ns, settings=s)
            assert out is not None
            assert "evt_only_in_other" not in out, (
                f"namespace 过滤失效：查到了别的 namespace 的节点（degree="
                f"{out['evt_only_in_other']}）"
            )

            # 同时确认自己的节点还能正常查到
            out2 = event_degrees(["evt_hub8"], t.ns, settings=s)
            assert out2 is not None and out2.get("evt_hub8") == 8
        finally:
            sess.run("MATCH (n) WHERE n.namespace = $ns DETACH DELETE n", ns=other_ns)


def test_event_degrees_missing_id_absent_from_map(temp_ns) -> None:
    """查不到的 id 不出现在字典里（调用方按 0 处理）。"""
    s, t = temp_ns
    t.make_hub_event("evt_hub8", 8)
    out = event_degrees(["evt_hub8", "evt_不存在"], t.ns, settings=s)
    assert out is not None
    assert "evt_不存在" not in out
    assert out["evt_hub8"] == 8


# ============================================================ 端到端：造图 → 算度 → 保护


def test_hub_protection_end_to_end(temp_ns) -> None:
    """完整链路：造 8 条边的图 → 真实算度 → classify 判 PROTECTED。

    这条把两张表里的洞一起补上：真实 Cypher + 真实决策。
    """
    s, t = temp_ns
    t.make_hub_event("evt_hub8", 8)

    degrees = event_degrees(["evt_hub8"], t.ns, settings=s)
    assert degrees is not None and degrees["evt_hub8"] == 8

    degree = degrees.get("evt_hub8", 0)
    v = classify(
        ForgetCandidate(
            event_id="evt_hub8",
            graph_degree=degree,  # ← 真实算出来的值，不是手填的 8
            created_at=OLD,
        ),
        today=date(2026, 11, 6),
        idle_days=180,
        min_recalls=1,
        grace_days=90,
        protect_importance=0.7,
        graph_hub_degree=s.GRAPH_HUB_DEGREE,
    )
    assert v.bucket == BUCKET_PROTECTED
    assert v.reason == "graph_hub"


def test_hub_boundary_seven_is_not_protected(temp_ns) -> None:
    """7 条边不保护 —— 边界必须二值可判，差一条就是「保护 / 可删」的分界。

    这条在 fake driver 时代测过（构造 graph_degree=7），
    但没测过「算出来正好是 7」的情况 —— 如果 Cypher 多算了一条，
    7 会变成 8 而误保护，fake driver 那条测试发现不了。
    """
    s, t = temp_ns
    t.make_hub_event("evt_hub7", 7)

    degrees = event_degrees(["evt_hub7"], t.ns, settings=s)
    assert degrees is not None
    assert degrees["evt_hub7"] == 7, "边界事件被多算了"

    v = classify(
        ForgetCandidate(
            event_id="evt_hub7",
            graph_degree=degrees["evt_hub7"],
            created_at=OLD,
        ),
        today=date(2026, 11, 6),
        idle_days=180,
        min_recalls=1,
        grace_days=90,
        protect_importance=0.7,
        graph_hub_degree=s.GRAPH_HUB_DEGREE,
    )
    assert not (v.bucket == BUCKET_PROTECTED and v.reason == "graph_hub"), (
        f"degree=7 被当成 hub 保护了（{v.bucket}/{v.reason}）—— 边界错位"
    )


def test_temp_namespace_cleanup_leaves_nothing(temp_ns) -> None:
    """造完清理，图里必须零残留。

    027 的教训：造测试数据的脚本不在 finally 里清，中断一次留一堆垃圾。
    这条在 fixture 里已经 assert 过一次，这里再显式验一遍清理路径本身。
    """
    s, t = temp_ns
    t.make_hub_event("evt_cleanup", 3)
    assert t.count() > 0, "造数据失败，这条测试没有意义"

    deleted = t.cleanup()
    assert deleted > 0
    assert t.count() == 0, f"清理后还有 {t.count()} 个节点"

# ============================================================ spec 034：RELATED 边不计入度数


def test_event_related_edges_do_not_count_as_degree(temp_ns) -> None:
    """**Event-Event 的 `RELATED` 边不计入度数**。

    这是真实数据（DingTalk 40 条）暴露的设计缺陷：

    `graph_outbox` 给「共享 Person/Entity」的两个 Event 连 `RELATED` 边，
    上限 `MAX_RELATED_LINKS=20`。于是一个只提到 1 个人的事件，只要那个人
    在 40 条记忆里都出现，就能拿到 20 条 RELATED 边。

    而 `GRAPH_HUB_DEGREE=8` 的意图是「**内容**特别丰富」（提到很多人/
    实体/动作），典型事件 4-6 条结构边。把 RELATED 算进去的结果是
    **40 个事件全部度数 16-25，全部判 graph_hub 保护，遗忘机制彻底失效**。

    030 的端到端测试用 `TESTLINK` 连 Time/Action/Entity/Person，
    **一条 Event-Event 边都没有**，所以那批测试测不出这个缺陷 ——
    合成图的盲区只能靠真实数据补。

    这条测试造「2 个结构邻居 + 8 个 Event 邻居」，断言度数 = 2。
    """
    s, t = temp_ns

    # 一个 Event，连 2 个非 Event 邻居
    t.make_hub_event("evt_with_related", 2)

    # 再连 8 个**其他 Event**（模拟 RELATED 边）
    with t.driver.session() as sess:
        for i in range(8):
            sess.run(
                """
                MATCH (e:Event {event_id: $eid, namespace: $ns})
                MERGE (o:Event {event_id: $oid, namespace: $ns})
                SET o.summary = '邻居事件'
                MERGE (e)-[:RELATED]->(o)
                """,
                eid="evt_with_related",
                ns=t.ns,
                oid=f"{t.ns}_other{i}",
            )

    out = event_degrees(["evt_with_related"], t.ns, settings=s)
    assert out is not None
    assert out.get("evt_with_related") == 2, (
        f"度数应为 2（只数非 Event 邻居），实际 {out.get('evt_with_related')} —— "
        "RELATED 边被算进去了，真实数据上会让所有事件都被 hub 保护"
    )
