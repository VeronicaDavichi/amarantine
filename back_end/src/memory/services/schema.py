# -*- coding: utf-8 -*-
"""SQLite schema 版本与迁移（spec 049 A4）。

## 现状与目标

现在每个服务各自 `CREATE TABLE IF NOT EXISTS` + 自己补列
（`raw_quote._ensure_added_columns`）。这能应付**加列**，但有两个洞：

1. **没有版本号** → 谁也说不清「我这个库是第几版」，
   备份还原到旧版代码上会静默少列。
2. **迁移逻辑散在 11 个模块里** → 新增一条「改数据」的迁移没有落点。

本模块给一个最小可用的框架：

- `PRAGMA user_version` 记版本（SQLite 自带，不占表）
- `MIGRATIONS` 是有序列表；每一步必须**幂等**（重跑不炸）
- `ensure_schema()` 在进程启动时调一次，把库升到 `SCHEMA_VERSION`
- 升级**只向前**：库版本高于代码时直接报错（用旧代码读新库会读出半个真相）

## 为什么不用 Alembic

本项目 SQLite 只有 12 张表、无外键、无 ORM。引一个 ORM 迁移框架的复杂度
大于收益；这里要的只是「有序、幂等、可查版本」。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger("memory.schema")

#: 当前代码期望的 schema 版本。**加一条迁移就 +1**。
SCHEMA_VERSION = 2


def _migrate_1_ingest_job_columns(conn: sqlite3.Connection) -> None:
    """v1：`ingest_job` 补 `dropped_events` / `split_rounds`（spec 036）。

    把原来散在 `raw_quote._ensure_added_columns` 的补列逻辑纳进版本体系 ——
    行为不变，但现在知道「这一库补过没」。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ingest_job)")}
    if not existing:
        # 表还没建（全新库）：交给各服务自己的 CREATE TABLE，跳过
        return
    for name, ddl in (
        ("dropped_events", "INTEGER NOT NULL DEFAULT 0"),
        ("split_rounds", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE ingest_job ADD COLUMN {name} {ddl}")
            logger.info("schema v1: ingest_job 补列 %s", name)


def _migrate_2_entity_suppress(conn: sqlite3.Connection) -> None:
    """v2：`entity_suppress` 建表 + 索引（spec 044）。

    这张表原本由 `entity_suppress.py` 懒建；放进迁移后，
    **即使某台机器从没写过被抑制的名字**，库里也有这张表 ——
    备份/还原与「查抑制清单」的行为才一致。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entity_suppress ("
        " namespace TEXT NOT NULL, name TEXT NOT NULL, reason TEXT NOT NULL,"
        " decided_by TEXT NOT NULL DEFAULT 'auto',"
        " status TEXT NOT NULL DEFAULT 'rejected',"
        " detail TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " PRIMARY KEY (namespace, name))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_suppress_status"
        " ON entity_suppress(namespace, status)"
    )


#: 有序迁移：`(目标版本, 说明, 函数)`。函数必须幂等。
MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "ingest_job 补 dropped_events / split_rounds", _migrate_1_ingest_job_columns),
    (2, "entity_suppress 建表 + 索引", _migrate_2_entity_suppress),
)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def ensure_schema(
    conn: sqlite3.Connection,
    *,
    target: int | None = None,
) -> tuple[int, list[str]]:
    """把库升到 `SCHEMA_VERSION`（或 `target`）。

    Returns:
        `(最终版本, 执行过的迁移说明列表)`。

    Raises:
        RuntimeError: 库版本**高于**代码期望时（旧代码读新库会读出半个真相）。
    """
    want = SCHEMA_VERSION if target is None else target
    have = current_version(conn)
    if have > want:
        raise RuntimeError(
            f"库 schema 版本 {have} 高于代码期望的 {want} —— "
            "用旧代码读新库会漏字段，请升级代码或还原旧备份"
        )
    applied: list[str] = []
    for version, desc, fn in MIGRATIONS:
        if version <= have or version > want:
            continue
        fn(conn)
        conn.execute(f"PRAGMA user_version = {version}")
        applied.append(f"v{version}: {desc}")
        logger.info("schema 迁移 → v%d: %s", version, desc)
    conn.commit()
    return current_version(conn), applied
