"""原文层：`raw_quote`（事实层原文）+ `ingest_job`（导入任务状态）。

两个表同库（`DATABASE_URL` 指向的 SQLite），与 `pending_task` / `recall_trace`
等派生表同口径。

**`raw_quote` 只增不改不删**：ADR A32 要求「软删事件卡后原文仍可读」，
所以这里连删除方法都不提供 —— 不给，比「给了但藏着」更难被误用。

**为什么不复用 `pending_task` 存导入任务**：`retry_pending()` 对**所有 kind**
都会 `write_event_node()` 重放。对话原文被当图写任务重放，会在图上刷出
一堆垃圾节点。省几行换来一个静默的破坏源，不值。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from memory.config import Settings
from memory.services.importer import assign_event_msg_times, relative_time_hint

from memory.services.sqlite_conn import open_db

logger = logging.getLogger("memory.raw_quote")

#: 任务状态机。`rejected` 是「整批被 do_not_store 拦下」，不是失败。
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_REJECTED = "rejected"
JOB_FAILED = "failed"
JOB_STATES = (JOB_QUEUED, JOB_RUNNING, JOB_DONE, JOB_REJECTED, JOB_FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_quote (
    namespace  TEXT NOT NULL,
    quote_id   TEXT NOT NULL,
    text       TEXT NOT NULL,
    role       TEXT NOT NULL,
    ts         TEXT,
    source     TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace, quote_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_quote_session
    ON raw_quote(namespace, session_id);

CREATE TABLE IF NOT EXISTS ingest_job (
    job_id         TEXT PRIMARY KEY,
    namespace      TEXT NOT NULL,
    dedupe_key     TEXT NOT NULL,
    status         TEXT NOT NULL,
    source         TEXT NOT NULL,
    session_id     TEXT,
    turns_total    INTEGER NOT NULL DEFAULT 0,
    turns_skipped  INTEGER NOT NULL DEFAULT 0,
    events_created INTEGER NOT NULL DEFAULT 0,
    dropped_events INTEGER NOT NULL DEFAULT 0,
    split_rounds   INTEGER NOT NULL DEFAULT 1,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_job_dedupe
    ON ingest_job(namespace, dedupe_key);
"""

#: 后续版本加的列。`CREATE TABLE IF NOT EXISTS` 对**已存在**的表是空操作，
#: 所以老库必须靠 `ALTER TABLE` 补齐 —— 否则升级后第一次写入就报 no such column。
#: 加列只允许「带默认值的 NOT NULL」或可空列，不能让老行变非法。
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    # spec 036：切分超限时丢了几条（0 = 一条没丢）
    ("dropped_events", "INTEGER NOT NULL DEFAULT 0"),
    # spec 036：切分用掉了几轮二分（1 = 没二分）
    ("split_rounds", "INTEGER NOT NULL DEFAULT 1"),
)


def _ensure_added_columns(conn: sqlite3.Connection) -> None:
    """给老库补列（幂等）。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(ingest_job)")}
    for name, ddl in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE ingest_job ADD COLUMN {name} {ddl}")
            logger.info("ingest_job 补列 %s %s", name, ddl)


def db_path_from_settings(settings: Settings) -> Path:
    """从 `DATABASE_URL` 推出 SQLite 文件路径（与 pending_task 同口径）。"""
    url = settings.DATABASE_URL
    if not url.startswith("sqlite"):
        raise ValueError(f"raw_quote 目前只支持 SQLite：{url}")
    raw = url.split("///", 1)[-1]
    return Path(raw).expanduser().resolve()


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


#: 允许的 turn role。白名单而不是自由字符串：`role` 会原样回给调用方，
#: 让任意值进来等于给了一个不受控的字段。
ALLOWED_ROLES = ("user", "assistant", "system", "tool")


def quote_id_for(
    *,
    namespace: str,
    source: str,
    session_id: str | None,
    ts: str | None,
    text: str,
) -> str:
    """给一条原始消息算稳定 id：`msg_` + sha256(...)[:16]。

    含 `namespace`：同一段对话导入两个域要各自可查、不互相覆盖。
    含 `text`：内容变了就是新 quote —— 事实层不可变，改了就该是新东西。

    稳定是幂等的基础：同一 turn 重复导入落到同一个 id，
    `INSERT OR IGNORE` 就天然去重，不需要额外的「已导入」判断。
    """
    canonical = "\x1f".join(
        [
            namespace,
            source or "",
            session_id or "",
            (ts or "").strip(),
            text or "",
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return "msg_" + digest


@dataclass(frozen=True)
class RawQuote:
    namespace: str
    quote_id: str
    text: str
    role: str
    ts: str | None
    source: str
    session_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "text": self.text,
            "role": self.role,
            "ts": self.ts,
            "source": self.source,
            "session_id": self.session_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class IngestJob:
    job_id: str
    namespace: str
    dedupe_key: str
    status: str
    source: str
    session_id: str | None
    turns_total: int
    turns_skipped: int
    events_created: int
    error: str | None
    created_at: str
    updated_at: str
    #: spec 036：切分超限丢掉的条数（0 = 一条没丢）
    dropped_events: int = 0
    #: spec 036：切分用掉的二分轮次（1 = 没二分）
    split_rounds: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "events_created": self.events_created,
            "turns_total": self.turns_total,
            "turns_skipped": self.turns_skipped,
            "dropped_events": self.dropped_events,
            "split_rounds": self.split_rounds,
            "source": self.source,
            "session_id": self.session_id,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class IngestOutcome:
    """一次后台导入的结果（只用于内部汇报与测试断言）。"""

    job_id: str
    events_created: int = 0
    turns_skipped: int = 0
    #: spec 036：切分超限丢掉的条数（0 = 一条没丢）
    dropped_events: int = 0
    #: spec 036：切分用掉的二分轮次（1 = 没二分）
    split_rounds: int = 1
    errors: list[str] = field(default_factory=list)


class RawQuoteStore:
    """事实层原文。只增不改不删。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(self._db_path)  # spec 024：并发参数由 DB_BUSY_TIMEOUT_MS 决定
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def put(
        self,
        *,
        namespace: str,
        quote_id: str,
        text: str,
        role: str,
        source: str,
        ts: str | None = None,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """写一条原文。返回 True 表示**新插入**，False 表示已存在。

        `INSERT OR IGNORE`：同 `quote_id` 不覆盖。事实层不可变 ——
        哪怕调用方重传了不同文本（id 由 hash 决定，文本变了 id 也会变），
        这里也不该悄悄改掉已有记录。
        """
        if role not in ALLOWED_ROLES:
            raise ValueError(f"role 必须是 {ALLOWED_ROLES} 之一，收到 {role!r}")
        if not text or not text.strip():
            raise ValueError("text 不能为空")
        moment = _now_iso(now)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO raw_quote"
                " (namespace, quote_id, text, role, ts, source, session_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    namespace,
                    quote_id,
                    text,
                    role,
                    ts,
                    source,
                    session_id,
                    moment,
                ),
            )
            return cur.rowcount > 0

    def get(self, namespace: str, quote_id: str) -> RawQuote | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM raw_quote WHERE namespace = ? AND quote_id = ?",
                (namespace, quote_id),
            ).fetchone()
        return _row_to_quote(row) if row is not None else None

    def count(self, namespace: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM raw_quote WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return int(row["n"]) if row is not None else 0


def _row_to_quote(row: sqlite3.Row) -> RawQuote:
    return RawQuote(
        namespace=row["namespace"],
        quote_id=row["quote_id"],
        text=row["text"],
        role=row["role"],
        ts=row["ts"],
        source=row["source"],
        session_id=row["session_id"],
        created_at=row["created_at"],
    )


class IngestJobStore:
    """导入任务状态。`UNIQUE(namespace, dedupe_key)` 是幂等的落点。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(self._db_path)  # spec 024：并发参数由 DB_BUSY_TIMEOUT_MS 决定
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            _ensure_added_columns(conn)

    def find_by_dedupe(self, namespace: str, dedupe_key: str) -> IngestJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_job WHERE namespace = ? AND dedupe_key = ?",
                (namespace, dedupe_key),
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def create(
        self,
        *,
        namespace: str,
        dedupe_key: str,
        source: str,
        session_id: str | None,
        turns_total: int,
        turns_skipped: int = 0,
        status: str = JOB_QUEUED,
        error: str | None = None,
        now: datetime | None = None,
    ) -> IngestJob:
        """建任务。同 `dedupe_key` 已存在时**返回已有那条**，不新建。

        唯一索引兜底并发：两个请求同时冲进来，另一个会拿到
        `IntegrityError`，这里重查一次把它变成「返回已有 job」，
        而不是把异常抛给调用方。
        """
        if status not in JOB_STATES:
            raise ValueError(f"status 必须是 {JOB_STATES} 之一")
        moment = _now_iso(now)
        job_id = "job_" + hashlib.sha256(
            f"{namespace}\x1f{dedupe_key}".encode("utf-8")
        ).hexdigest()[:16]
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO ingest_job"
                    " (job_id, namespace, dedupe_key, status, source, session_id,"
                    "  turns_total, turns_skipped, events_created, error,"
                    "  created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (
                        job_id,
                        namespace,
                        dedupe_key,
                        status,
                        source,
                        session_id,
                        turns_total,
                        turns_skipped,
                        error,
                        moment,
                        moment,
                    ),
                )
        except sqlite3.IntegrityError:
            existing = self.find_by_dedupe(namespace, dedupe_key)
            if existing is None:  # 唯一索引冲突但查不到，只可能是别的问题
                raise
            return existing
        got = self.get(job_id)
        if got is None:  # 理论上不可能
            raise RuntimeError(f"刚写入的任务读不回来: {job_id}")
        return got

    def get(self, job_id: str) -> IngestJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_job WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def set_status(
        self,
        job_id: str,
        *,
        status: str,
        events_created: int | None = None,
        turns_skipped: int | None = None,
        dropped_events: int | None = None,
        split_rounds: int | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """更新状态。`error=None` 表示**不改** error（用显式清空代替不了，
        但保留旧错误比误清更有用 —— 失败过一次的记录不该因为后续成功就消失）。
        """
        if status not in JOB_STATES:
            raise ValueError(f"status 必须是 {JOB_STATES} 之一")
        sets = ["status = ?", "updated_at = ?"]
        args: list[Any] = [status, _now_iso(now)]
        if events_created is not None:
            sets.append("events_created = ?")
            args.append(events_created)
        if turns_skipped is not None:
            sets.append("turns_skipped = ?")
            args.append(turns_skipped)
        # spec 036：两个观测字段也要能落库，否则「丢了几条」只活在日志里
        if dropped_events is not None:
            sets.append("dropped_events = ?")
            args.append(dropped_events)
        if split_rounds is not None:
            sets.append("split_rounds = ?")
            args.append(split_rounds)
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        args.append(job_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE ingest_job SET {', '.join(sets)} WHERE job_id = ?", args
            )


def _row_to_job(row: sqlite3.Row) -> IngestJob:
    keys = row.keys()
    return IngestJob(
        job_id=row["job_id"],
        namespace=row["namespace"],
        dedupe_key=row["dedupe_key"],
        status=row["status"],
        source=row["source"],
        session_id=row["session_id"],
        turns_total=row["turns_total"],
        turns_skipped=row["turns_skipped"],
        events_created=row["events_created"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        # 兜底：万一拿到的是迁移前的库（没走 `_ensure_added_columns`），
        # 也按「没丢、没二分」读，而不是让整个 GET 500。
        dropped_events=int(row["dropped_events"]) if "dropped_events" in keys else 0,
        split_rounds=int(row["split_rounds"]) if "split_rounds" in keys else 1,
    )


# ---------------------------------------------------------------- 导入编排


def normalize_turn(raw: Any) -> dict[str, Any] | None:
    """校验并规整一个 turn。非法返回 None（由调用方计入 skipped）。

    `do_not_store` 不在这里处理 —— 它决定「要不要产卡」，
    是编排层的事，规整层只负责字段形状。
    """
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    role = raw.get("role") or "user"
    if not isinstance(text, str) or not text.strip():
        return None
    if role not in ALLOWED_ROLES:
        return None
    ts = raw.get("ts")
    return {
        "text": text,
        "role": role,
        "ts": ts if isinstance(ts, str) and ts.strip() else None,
        "do_not_store": bool(raw.get("do_not_store", False)),
    }


#: embedding 服务的单条上限是 8192 token；中文约 1 字 ≈ 1 token，
#: 留足余量按 4000 字符切。超过这个长度的「一张卡」一定是 LLM 切分降级
#: 成整段了（实测 10334 token 被 413 拒掉）。
MAX_CARD_CHARS = 4000


#: 过度切分阈值：产出事件数 > 输入条数 × 这个倍数就告警。
#:
#: 实测（QQ 语料）：30 条短消息被切成 141 个事件（80 入库 + 61 丢弃）。
#: 这不是 036 的「丢尾部」，而是模型把每条消息都拆碎了 —— 卡太小、
#: 召回价值低，还会把 embedding 和 LLM 预算吃光。
OVERSPLIT_RATIO = 3


def _split_long_parts(parts: list[str], *, limit: int = MAX_CARD_CHARS) -> list[str]:
    """把超长事件按**行**再切，避免 embedding 413。

    优先按换行切（语料是 `role: text` 一行一条），单行仍超长时硬切。
    切出来的每段都是独立事件，时间继承仍然按 4-gram 匹配各自源消息。
    """
    out: list[str] = []
    for part in parts:
        text = (part or "").strip()
        if not text:
            continue
        if len(text) <= limit:
            out.append(text)
            continue
        buf: list[str] = []
        size = 0
        for line in text.split("\n"):
            piece = line.strip()
            if not piece:
                continue
            while len(piece) > limit:
                out.append(piece[:limit])
                piece = piece[limit:]
            if size + len(piece) + 1 > limit and buf:
                out.append("\n".join(buf))
                buf, size = [], 0
            buf.append(piece)
            size += len(piece) + 1
        if buf:
            out.append("\n".join(buf))
    return out or [p for p in parts if (p or "").strip()]


def _parse_ts(value: str | None) -> datetime | None:
    """把语料里的 `YYYY-MM-DD HH:MM:SS` 解析成**带时区**的 datetime。

    聊天记录里的时间都是本地时间（`+08:00`）。必须带时区 —— Weaviate 的
    date 属性只收 RFC3339，裸 datetime 会被 422 拒掉（实测报错：
    `invalid date property 'msg_time' ... requires a string with a RFC3339`）。
    """
    if not value:
        return None
    local = timezone(timedelta(hours=8))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=local)
        except ValueError:
            continue
    return None


def run_ingest(
    *,
    job_id: str,
    namespace: str,
    turns: list[dict[str, Any]],
    quote_ids: list[str],
    settings: Settings,
    raw_store: RawQuoteStore | None = None,
    job_store: IngestJobStore | None = None,
) -> IngestOutcome:
    """后台把一批 turn 变成事件卡。

    **单条失败不整批 failed**：200 条里第 50 条超时，不该让前 49 条白干。
    只有「连切分都做不了」这种批级故障才置 failed。

    切分用 `split_all()`（spec 036）：超上限会二分而不是丢尾部。
    它自己会把失败降级成 `[整段文本]`，所以这里不需要 try 包切分 ——
    但它也可能因为配置缺失直接抛，那种才是批级故障。
    """
    from memory.services.llm_split import split_all
    from memory.services.remember import remember_event

    raw_store = raw_store or RawQuoteStore(db_path_from_settings(settings))
    job_store = job_store or IngestJobStore(db_path_from_settings(settings))
    job_store.set_status(job_id, status=JOB_RUNNING)

    outcome = IngestOutcome(job_id=job_id)
    usable = [t for t in turns if not t.get("do_not_store")]
    outcome.turns_skipped = len(turns) - len(usable)

    if not usable:
        # 防御性：入口已经拦过全批 do_not_store，这里再兜一层
        job_store.set_status(
            job_id,
            status=JOB_REJECTED,
            events_created=0,
            turns_skipped=outcome.turns_skipped,
            error="do_not_store",
        )
        return outcome

    joined = "\n".join(f'{t["role"]}: {t["text"]}' for t in usable)
    try:
        split = split_all(joined, settings)
    except Exception as exc:  # noqa: BLE001 - 批级故障才把整批标 failed
        outcome.errors.append(f"{type(exc).__name__}: {exc}")
        job_store.set_status(
            job_id,
            status=JOB_FAILED,
            turns_skipped=outcome.turns_skipped,
            error=f"切分失败: {exc}",
        )
        logger.warning("ingest 切分失败 %s: %s", job_id, exc)
        return outcome

    # spec 036/040：切分降级成整段时可能超 embedding 上限，按行再切
    summaries = _split_long_parts(split.parts)
    outcome.split_rounds = split.rounds
    outcome.dropped_events = split.dropped

    # 过度切分告警：产出远多于输入时的信号（不改变行为，只留痕）
    if usable and len(summaries) > max(10, OVERSPLIT_RATIO * len(usable)):
        msg = (
            f"切分过度: {len(usable)} 条输入 → {len(summaries)} 个事件"
            f"（阈值 {OVERSPLIT_RATIO}x）"
        )
        logger.warning("ingest %s | job=%s", msg, job_id)
        outcome.errors.append(msg)

    # 两层各记一条：切分层（split_all）说明「丢在哪、丢了多少」，
    # 这里补上「是哪个 job 丢的」—— 排查时只凭其中一条都定位不到责任批次。
    if split.dropped:
        logger.warning(
            "ingest 切分丢弃事件 | job=%s kept=%d dropped=%d rounds=%d",
            job_id,
            len(summaries),
            split.dropped,
            split.rounds,
        )

    # spec 040 T7：把事件映射回源消息日期。
    #
    # `date_start` 以前一律写成入库日（229 张卡的旧账），时间过滤直接失真。
    # 判据在 `importer.assign_event_dates`：摘要与 turn 的汉字 4-gram 交集。
    # **判不出就给 None**（A7），绝不回落到「今天」。
    # spec 040：消息时间与事件时间分开传。
    #
    # - `msg_time`：这条事件来自哪条消息（用于相对时间换算，也单独存）
    # - `date_hint`：摘要里的相对时间词（`昨天`/`上周`），由服务端按 msg_time 换算
    #
    # 原来的写法把「消息日期」当 `date_hint` 传，混淆了两件事：
    # 10-30 的消息说「昨天」，事件应该是 10-29，不是 10-30。
    event_msg_times = assign_event_msg_times(summaries, usable)

    for idx, summary in enumerate(summaries):
        try:
            raw_ts = event_msg_times[idx] if idx < len(event_msg_times) else None
            msg_time = _parse_ts(raw_ts)
            remember_event(
                namespace=namespace,
                summary=summary,
                date_hint=relative_time_hint(summary),
                msg_time=msg_time,
                settings=settings,
                raw_quote_ids=list(quote_ids),
            )
            outcome.events_created += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败只跳过
            # **不**把它计进 `turns_skipped`：那一栏记的是「被 do_not_store
            # 拦下的 turn 数」，写卡失败是「摘要没变成卡」，两码事。
            # 混在一起，`events_created + turns_skipped` 就凑不出可解释的总数，
            # 沉默损失（errors 里那些）也就看不见了。
            outcome.errors.append(f"第 {idx + 1} 条: {type(exc).__name__}: {exc}")
            logger.warning("ingest 单条写卡失败 %s #%d: %s", job_id, idx + 1, exc)

    final = JOB_DONE if outcome.events_created > 0 else JOB_FAILED
    job_store.set_status(
        job_id,
        status=final,
        events_created=outcome.events_created,
        turns_skipped=outcome.turns_skipped,
        dropped_events=outcome.dropped_events,
        split_rounds=outcome.split_rounds,
        error="; ".join(outcome.errors[:3]) if outcome.errors else None,
    )
    return outcome
