"""意图日志：写前记账、成功销账、失败补跑、对账。

这组测试守的是一条硬约束：

    **不能依赖「关闭时 flush」。**

MCP 进程的死法大多抓不到（kill -9 / WSL 重启 / taskkill /F / OOM），
`atexit` 和 SIGTERM 在那些情况下一次都不执行。所以正确性只能靠
「死在任何一点，重启后从盘上自证状态接着走」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memory.services.pending_task import (
    KIND_GRAPH_REBUILD,
    KIND_GRAPH_WRITE,
    MAX_ATTEMPTS,
    PendingTaskStore,
)

NOW = datetime(2026, 11, 2, 8, 0, 0, tzinfo=timezone.utc)
NS = "work"


@pytest.fixture
def store(tmp_path) -> PendingTaskStore:
    return PendingTaskStore(tmp_path / "intent.sqlite3")


# ---------------- 记账 / 销账 ----------------

def test_enqueue_returns_id(store: PendingTaskStore) -> None:
    tid = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    assert tid > 0


def test_enqueue_is_deduped(store: PendingTaskStore) -> None:
    """同一个事件重复入队只留一行，返回原来那行的 id。

    否则一次失败的写入会在每次重试后多出一行，几百行指向同一个事件。
    """
    first = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    second = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    assert first == second
    assert store.stats(NS)["pending"] == 1


def test_complete_deletes_row(store: PendingTaskStore) -> None:
    tid = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    assert store.complete(tid) is True
    assert store.stats(NS)["total"] == 0


def test_complete_unknown_id_is_false(store: PendingTaskStore) -> None:
    assert store.complete(99999) is False


def test_new_task_is_immediately_ready(store: PendingTaskStore) -> None:
    """刚记下的账要能被立刻补跑（next_retry_at 就是入队时刻）。"""
    store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}}, now=NOW)
    ready = store.ready(NS, now=NOW)
    assert len(ready) == 1
    assert ready[0].dedupe_key == "evt_1"


def test_deduped_row_keeps_original_payload(store: PendingTaskStore) -> None:
    """第二次入队不覆盖第一次的 payload —— 第一份才是出问题时的现场。"""
    store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {"v": 1}})
    store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {"v": 2}})
    assert store.ready(NS)[0].payload["card"]["v"] == 1


# ---------------- 失败与退避 ----------------

def test_fail_schedules_retry(store: PendingTaskStore) -> None:
    tid = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}}, now=NOW)
    status = store.fail(tid, "neo4j down", now=NOW)
    assert status == "pending"
    task = store.get(tid)
    assert task.attempts == 1
    assert task.last_error == "neo4j down"
    # 退避后不再「立刻」可跑
    assert store.ready(NS, now=NOW) == []
    assert len(store.ready(NS, now=NOW + timedelta(minutes=5))) == 1


def test_backoff_grows(store: PendingTaskStore) -> None:
    """指数退避：第二次的等待比第一次长。"""
    tid = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}}, now=NOW)
    store.fail(tid, "e1", now=NOW)
    first = datetime.fromisoformat(store.get(tid).next_retry_at)
    store.fail(tid, "e2", now=NOW)
    second = datetime.fromisoformat(store.get(tid).next_retry_at)
    assert (first - NOW) < (second - NOW)


def test_fail_marks_dead_at_limit(store: PendingTaskStore) -> None:
    """到上限标 dead，停手。"""
    tid = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}}, now=NOW)
    for _ in range(MAX_ATTEMPTS - 1):
        assert store.fail(tid, "boom", now=NOW) == "pending"
    assert store.fail(tid, "boom", now=NOW) == "dead"


def test_dead_rows_are_never_deleted(store: PendingTaskStore) -> None:
    """**放弃补跑不等于删掉记录。**

    删了就真没痕迹了 —— 留一行才知道「有过这件事、没办成」。
    """
    tid = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}}, now=NOW)
    for _ in range(MAX_ATTEMPTS):
        store.fail(tid, "boom", now=NOW)
    dead = store.dead(NS)
    assert len(dead) == 1
    assert dead[0].dedupe_key == "evt_1"
    assert dead[0].last_error == "boom"
    # dead 不再出现在 ready 里，但也没被删
    assert store.ready(NS, now=NOW + timedelta(days=30)) == []
    assert store.stats(NS)["total"] == 1


def test_dead_row_can_still_be_completed(store: PendingTaskStore) -> None:
    """人工排障后手动补一次、补上了，仍然应该能销账。"""
    tid = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}}, now=NOW)
    for _ in range(MAX_ATTEMPTS):
        store.fail(tid, "boom", now=NOW)
    assert store.complete(tid) is True
    assert store.stats(NS)["total"] == 0


# ---------------- 隔离 ----------------

def test_namespaces_are_isolated(tmp_path) -> None:
    store = PendingTaskStore(tmp_path / "x.sqlite3")
    store.enqueue("work", KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    store.enqueue("home", KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    assert store.stats("work")["pending"] == 1
    assert store.stats("home")["pending"] == 1
    # 同一个 dedupe_key 在不同 namespace 下是两行
    assert store.stats("work")["total"] + store.stats("home")["total"] == 2


def test_kind_is_part_of_dedupe(tmp_path) -> None:
    """`graph_write` 和 `graph_rebuild` 对同一事件是两件事。"""
    store = PendingTaskStore(tmp_path / "x.sqlite3")
    a = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    b = store.enqueue(NS, KIND_GRAPH_REBUILD, "evt_1", {"event_id": "evt_1"})
    assert a != b
    assert store.stats(NS)["pending"] == 2


# ---------------- 持久化语义 ----------------

def test_survives_reopen(tmp_path) -> None:
    """**换一个实例（等价于重启进程）账还在。**"""
    path = tmp_path / "intent.sqlite3"
    PendingTaskStore(path).enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    reopened = PendingTaskStore(path)
    assert reopened.stats(NS)["pending"] == 1


def test_payload_roundtrip_keeps_unicode(tmp_path) -> None:
    store = PendingTaskStore(tmp_path / "x.sqlite3")
    store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"summary": "张三决定项目A延期"})
    assert store.ready(NS)[0].payload["summary"] == "张三决定项目A延期"


def test_stats_shape(store: PendingTaskStore) -> None:
    assert store.stats(NS) == {"pending": 0, "dead": 0, "total": 0}
    tid = store.enqueue(NS, KIND_GRAPH_WRITE, "evt_1", {"card": {}})
    assert store.stats(NS) == {"pending": 1, "dead": 0, "total": 1}
    for _ in range(MAX_ATTEMPTS):
        store.fail(tid, "x")
    assert store.stats(NS) == {"pending": 0, "dead": 1, "total": 1}
