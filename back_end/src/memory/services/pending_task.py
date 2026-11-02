"""图写入的意图日志：写前记账，成功销账，失败补跑。

## 为什么不是「关闭时 flush」

MCP stdio 进程的死法大多**抓不到**：

| 能抓（但不可靠） | 抓不到 |
|---|---|
| stdin 关闭 | `kill -9` / `taskkill /F` |
| SIGTERM | **WSL 整个 VM 重启** |
| SIGINT | OOM killer |
| `atexit` | 断电 / 宿主机崩 |

`atexit` 和信号处理器在右栏**一次都不会执行**。所以正确性不能押在
「关闭时把没写完的刷下去」，只能押在「死在任何一点，重启后从盘上
自证状态接着走」。

（`grep signal/atexit/finally entrypoints/` 的结果是空的 —— 现在连
左栏那四个也没抓。即便如此也不该去补：补了也只是把正确性押在
一个我们无权控制的时刻上。）

## 三层保险

| 层 | 管什么 | 代价 |
|---|---|---|
| 意图日志 | 图写入失败 | O(1) 每次写 |
| 启动补跑 | 进程死在中途 | 扫一次表 |
| 对账扫描 | 跨库毫秒窗口 | O(n)，所以每天一次 |

**Weaviate 和 Neo4j 之间没有跨库事务。** 「Weaviate 写完、意图日志
还没落」之间有个毫秒级窗口，漏了就是漏了。这一层只能靠对账兜。

## 为什么不是真队列

不做投递语义、不做 worker 池。就是一张表 + 一次同步尝试 +
启动时扫一遍。`memory_remember` 的 happy path **一秒没变慢**：
图写入仍然同步、仍然在返回前完成，只是前后各多一条 SQLite 写入。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory.config import Settings

logger = logging.getLogger("memory.pending_task")

#: 单条最多重试几次，超了标 `dead` 留在表里等人看
MAX_ATTEMPTS = 5
#: 首次重试延迟（秒），之后指数退避
BASE_DELAY_S = 30.0
#: 退避上限（秒）
MAX_DELAY_S = 3600.0

#: 图节点写入（正常路径失败时补跑）
KIND_GRAPH_WRITE = "graph_write"
#: 从 Weaviate 反推补图（对账发现缺口时用，保真度较低）
KIND_GRAPH_REBUILD = "graph_rebuild"

_STATUS_PENDING = "pending"
_STATUS_DEAD = "dead"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_task (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace     TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    dedupe_key    TEXT    NOT NULL,
    payload       TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    next_retry_at TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_dedupe
    ON pending_task(namespace, kind, dedupe_key);
CREATE INDEX IF NOT EXISTS idx_pending_ready
    ON pending_task(status, next_retry_at);
"""


def db_path_from_settings(settings: Settings) -> Path:
    """从 `DATABASE_URL` 推出 SQLite 文件路径（与 recall_count 同口径）。"""
    url = settings.DATABASE_URL
    if not url.startswith("sqlite"):
        raise ValueError(f"pending_task 目前只支持 SQLite：{url}")
    raw = url.split("///", 1)[-1]
    return Path(raw).expanduser().resolve()


def _now_iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


@dataclass(frozen=True)
class Task:
    """一条待补跑的任务。"""

    id: int
    namespace: str
    kind: str
    dedupe_key: str
    payload: dict
    status: str
    attempts: int
    last_error: str | None
    created_at: str
    next_retry_at: str


@dataclass
class RetryReport:
    """一次补跑的结果。"""

    attempted: int = 0
    recovered: int = 0
    still_pending: int = 0
    dead: int = 0

    @property
    def ok(self) -> bool:
        return self.dead == 0


class PendingTaskStore:
    """意图日志表。

    每次操作各开一个连接（WAL 模式下并发读写没问题），不持有长连接 ——
    服务重启后不需要重建任何状态，盘上的表就是全部状态。
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._ensure_schema()

    # ---------- 建表 ----------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ---------- 记账 / 销账 ----------

    def enqueue(
        self,
        namespace: str,
        kind: str,
        dedupe_key: str,
        payload: dict,
        *,
        now: datetime | None = None,
    ) -> int:
        """记一笔待办，返回任务 id。

        **必须在动手之前调**。回头补写的账不叫账 —— 崩溃点正好在
        「动手之后、记账之前」时什么都没留下。

        `dedupe_key` 有唯一索引：同一个事件重复入队只会留一行，
        返回的仍是原来那行的 id。
        """
        moment = _now_iso(now)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO pending_task
                    (namespace, kind, dedupe_key, payload, status,
                     attempts, created_at, updated_at, next_retry_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    namespace,
                    kind,
                    dedupe_key,
                    json.dumps(payload, ensure_ascii=False),
                    moment,
                    moment,
                    moment,
                ),
            )
            row = conn.execute(
                "SELECT id FROM pending_task "
                "WHERE namespace = ? AND kind = ? AND dedupe_key = ?",
                (namespace, kind, dedupe_key),
            ).fetchone()
        return int(row[0]) if row else -1

    def complete(self, task_id: int) -> bool:
        """销账。**唯一会删行的操作**，只在真写成功之后调。"""
        if task_id < 0:
            return False
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM pending_task WHERE id = ?", (task_id,))
        return cur.rowcount > 0

    def fail(
        self,
        task_id: int,
        error: str,
        *,
        now: datetime | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> str:
        """记一次失败，返回新状态（`pending` / `dead`）。

        到 `max_attempts` 就标 `dead` 停手，但**行留在表里** ——
        删掉就真没痕迹了，留一行才知道「有过这件事、没办成」。
        """
        if task_id < 0:
            return _STATUS_DEAD
        moment = datetime.now(timezone.utc) if now is None else now
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM pending_task WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return _STATUS_DEAD
            attempts = int(row[0]) + 1
            if attempts >= max_attempts:
                conn.execute(
                    "UPDATE pending_task SET attempts = ?, last_error = ?, "
                    "status = 'dead', updated_at = ? WHERE id = ?",
                    (attempts, error[:2000], moment.isoformat(), task_id),
                )
                return _STATUS_DEAD
            delay = min(BASE_DELAY_S * (2 ** (attempts - 1)), MAX_DELAY_S)
            next_at = (moment + timedelta(seconds=delay)).isoformat()
            conn.execute(
                "UPDATE pending_task SET attempts = ?, last_error = ?, "
                "updated_at = ?, next_retry_at = ? WHERE id = ?",
                (attempts, error[:2000], moment.isoformat(), next_at, task_id),
            )
        return _STATUS_PENDING

    # ---------- 查询 ----------

    @staticmethod
    def _row_to_task(row) -> Task:
        return Task(
            id=int(row[0]),
            namespace=row[1],
            kind=row[2],
            dedupe_key=row[3],
            payload=json.loads(row[4]),
            status=row[5],
            attempts=int(row[6]),
            last_error=row[7],
            created_at=row[8],
            next_retry_at=row[10],
        )

    _SELECT = (
        "SELECT id, namespace, kind, dedupe_key, payload, status, attempts, "
        "last_error, created_at, updated_at, next_retry_at FROM pending_task"
    )

    def ready(
        self,
        namespace: str,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[Task]:
        """到期该补跑的任务（**包括从没试过的**）。"""
        moment = _now_iso(now)
        with self._connect() as conn:
            rows = conn.execute(
                self._SELECT
                + " WHERE namespace = ? AND status = 'pending' "
                "AND next_retry_at <= ? ORDER BY id LIMIT ?",
                (namespace, moment, int(limit)),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def dead(self, namespace: str, *, limit: int = 100) -> list[Task]:
        """放弃补跑的（重试超限）。只读，不删。"""
        with self._connect() as conn:
            rows = conn.execute(
                self._SELECT
                + " WHERE namespace = ? AND status = 'dead' ORDER BY id LIMIT ?",
                (namespace, int(limit)),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def get(self, task_id: int) -> Task | None:
        with self._connect() as conn:
            row = conn.execute(self._SELECT + " WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def stats(self, namespace: str) -> dict[str, int]:
        """`{"pending": n, "dead": n, "total": n}`。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM pending_task "
                "WHERE namespace = ? GROUP BY status",
                (namespace,),
            ).fetchall()
        out = {row[0]: int(row[1]) for row in rows}
        out.setdefault(_STATUS_PENDING, 0)
        out.setdefault(_STATUS_DEAD, 0)
        out["total"] = out[_STATUS_PENDING] + out[_STATUS_DEAD]
        return out

    def clear(self, namespace: str | None = None) -> None:
        """测试用：清空。"""
        with self._connect() as conn:
            if namespace is None:
                conn.execute("DELETE FROM pending_task")
            else:
                conn.execute("DELETE FROM pending_task WHERE namespace = ?", (namespace,))


# ---------------- 补跑 ----------------


def _rebuild_card(kind: str, payload: dict):
    """从 payload 还原 EventCard。

    两种情况：
    - `graph_write`：payload 是完整卡片，还原后**完全等值**于原始写入。
    - `graph_rebuild`：payload 只有 Weaviate 存得下的字段（没有
      persons/actions/alias_claims —— 那些 Weaviate 里根本没有）。
      保真度低，只能补出 Event + Time + MENTIONS。
    """
    from memory.models.event_card import EventCard

    if kind == KIND_GRAPH_WRITE:
        return EventCard.model_validate(payload["card"])

    # graph_rebuild：拿 Weaviate 的字段拼一张最小卡
    #
    # `date_start` 是必填（不是 Optional）。Weaviate 里如果有就用，
    # 没有就退到 `ingested_at` 的日期 —— 补出来的时间轴会偏，
    # 但比丢掉整条图关系好，而且 `graph_rebuild` 这个 kind 标着「保真度低」。
    ingested_at = payload.get("ingested_at") or _now_iso(None)
    date_start = payload.get("date_start") or ingested_at[:10]
    return EventCard(
        namespace=payload["namespace"],
        event_id=payload["event_id"],
        summary=payload.get("summary") or "",
        types=payload.get("types") or ["other"],
        retention=payload.get("retention") or "normal",
        ingested_at=ingested_at,
        date_start=date_start,
        date_end=payload.get("date_end"),
        entities=[
            {"name": n, "kind": "topic"} for n in (payload.get("entity_names") or [])
        ],
    )


def retry_pending(
    namespace: str,
    *,
    settings: Settings,
    store: PendingTaskStore | None = None,
    limit: int = 50,
    now: datetime | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> RetryReport:
    """把到期的待办各补跑一次。

    幂等：`event_id` 是内容哈希，重放同一张卡在图上落到同一个节点。
    `MERGE ... ON CREATE SET` 保证属性不会被覆盖。
    """
    from memory.services.graph_outbox import write_event_node

    store = store or PendingTaskStore(db_path_from_settings(settings))
    report = RetryReport()
    tasks = store.ready(namespace, now=now, limit=limit)
    for task in tasks:
        report.attempted += 1
        try:
            card = _rebuild_card(task.kind, task.payload)
            result = write_event_node(
                card,
                settings=settings,
                weaviate_id=task.payload.get("weaviate_id"),
                context=task.payload.get("context"),
            )
            ok = bool(getattr(result, "ok", False))
            error = getattr(result, "error", None) or "write_event_node 返回 ok=False"
        except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮整批
            ok = False
            error = f"{type(exc).__name__}: {exc}"

        if ok:
            store.complete(task.id)
            report.recovered += 1
            logger.info("补跑成功 task=%s event=%s", task.id, task.dedupe_key)
        else:
            status = store.fail(
                task.id, error, now=now, max_attempts=max_attempts
            )
            if status == _STATUS_DEAD:
                report.dead += 1
                logger.error(
                    "补跑放弃 task=%s event=%s attempts=%d: %s",
                    task.id,
                    task.dedupe_key,
                    task.attempts + 1,
                    error,
                )
            else:
                report.still_pending += 1
                logger.warning(
                    "补跑失败 task=%s event=%s attempts=%d: %s",
                    task.id,
                    task.dedupe_key,
                    task.attempts + 1,
                    error,
                )
    return report


# ---------------- 对账 ----------------


@dataclass
class ReconcileReport:
    """对账结果。"""

    checked: int = 0
    #: Weaviate 里有、图上没有的 event_id
    missing: list[str] = None  # type: ignore[assignment]
    #: 已入队等补跑的条数
    queued: int = 0

    def __post_init__(self) -> None:
        if self.missing is None:
            self.missing = []


def reconcile_graph(
    namespace: str,
    *,
    settings: Settings,
    store: PendingTaskStore | None = None,
    weaviate: object | None = None,
    driver: object | None = None,
    limit: int = 500,
    now: datetime | None = None,
) -> ReconcileReport:
    """找「Weaviate 有、Neo4j 没有」的事件，入队补图。

    **这一层是兜跨库毫秒窗口的**：进程正好死在「Weaviate 写成功、
    意图日志还没落」之间时，日志里没有痕迹，只有对账能发现。

    **保真度有限**：Weaviate 不存 persons / actions / alias_claims
    （`SCHEMA_PROPERTIES` 里没有这些字段），所以补出来的是
    Event + Time + MENTIONS，**INVOLVES / ACTION 补不回来**。
    这在报告里用 `graph_rebuild` 这个 kind 标着，能被查出来。
    """
    from memory.services.weaviate_store import WeaviateStore

    store = store or PendingTaskStore(db_path_from_settings(settings))
    report = ReconcileReport()

    owns_weaviate = weaviate is None
    wv = weaviate or WeaviateStore(settings)
    owns_driver = driver is None
    drv = driver
    try:
        if owns_driver:
            from neo4j import GraphDatabase

            drv = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            )

        records = wv.list_all(namespace, limit=limit)  # type: ignore[attr-defined]
        report.checked = len(records)
        if not records:
            return report

        event_ids = [r.get("event_id") for r in records if r.get("event_id")]
        with drv.session() as session:  # type: ignore[union-attr]
            rows = session.run(
                "MATCH (e:Event {namespace: $namespace}) "
                "WHERE e.event_id IN $event_ids RETURN e.event_id AS event_id",
                namespace=namespace,
                event_ids=event_ids,
            )
            on_graph = {r["event_id"] for r in rows}

        for record in records:
            eid = record.get("event_id")
            if not eid or eid in on_graph:
                continue
            report.missing.append(eid)
            store.enqueue(
                namespace,
                KIND_GRAPH_REBUILD,
                eid,
                {
                    "namespace": namespace,
                    "event_id": eid,
                    "summary": record.get("summary"),
                    "types": record.get("types"),
                    "retention": record.get("retention"),
                    "ingested_at": record.get("ingested_at"),
                    "date_start": record.get("date_start"),
                    "date_end": record.get("date_end"),
                    "entity_names": record.get("entity_names"),
                    "weaviate_id": record.get("id"),
                },
                now=now,
            )
            report.queued += 1

        if report.missing:
            logger.warning(
                "对账发现 %d 条事件缺图节点（已入队补跑，INVOLVES/ACTION 补不回来）: %s",
                len(report.missing),
                report.missing[:10],
            )
        return report
    except Exception as exc:  # noqa: BLE001 - 对账失败不该影响主流程
        logger.warning("对账失败（已忽略）: %s", exc)
        return report
    finally:
        if owns_weaviate:
            try:
                wv.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
        if owns_driver and drv is not None:
            try:
                drv.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
