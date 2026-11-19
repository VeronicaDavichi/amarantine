"""spec 049 A4：SQLite schema 版本与迁移。"""

from __future__ import annotations

import sqlite3

import pytest

from memory.services.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    current_version,
    ensure_schema,
)


def _old_db(tmp_path) -> sqlite3.Connection:
    """造一个「老库」：有 ingest_job 但缺后来加的列，且 user_version=0。"""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute(
        "CREATE TABLE ingest_job (job_id TEXT PRIMARY KEY, namespace TEXT NOT NULL,"
        " status TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _cols(conn, table) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_bumps_to_target(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "new.db")
    assert current_version(conn) == 0
    ver, applied = ensure_schema(conn)
    assert ver == SCHEMA_VERSION
    assert applied, "全新库也应跑过迁移（至少建 entity_suppress）"
    conn.close()


def test_old_db_gets_missing_columns(tmp_path) -> None:
    conn = _old_db(tmp_path)
    assert "dropped_events" not in _cols(conn, "ingest_job")
    ensure_schema(conn)
    cols = _cols(conn, "ingest_job")
    assert "dropped_events" in cols and "split_rounds" in cols
    conn.close()


def test_entity_suppress_created(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "n.db")
    ensure_schema(conn)
    cols = _cols(conn, "entity_suppress")
    assert {"namespace", "name", "reason", "status"} <= cols
    conn.close()


def test_idempotent(tmp_path) -> None:
    conn = _old_db(tmp_path)
    ensure_schema(conn)
    ver, applied = ensure_schema(conn)
    assert ver == SCHEMA_VERSION
    assert applied == [], "已是最新时不该再跑迁移"
    conn.close()


def test_refuses_downgrade(tmp_path) -> None:
    """库版本高于代码 → 报错，不静默继续（旧代码读新库会漏字段）。"""
    conn = sqlite3.connect(tmp_path / "future.db")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.commit()
    with pytest.raises(RuntimeError, match="高于代码期望"):
        ensure_schema(conn)
    conn.close()


def test_migrations_are_ordered_and_unique() -> None:
    versions = [v for v, _d, _f in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[-1] == SCHEMA_VERSION, "SCHEMA_VERSION 必须等于最后一条迁移"
