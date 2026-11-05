"""spec 024：SQLite 并发参数必须真的跟着配置走。

这个文件存在的理由是**一条特定的 bug 类型**：

    `sqlite3.connect(path, timeout=5.0)`  +  `DB_BUSY_TIMEOUT_MS=5000`

`timeout=5.0` 恰好等于 Python 的默认值，也恰好等于配置默认值。
于是观察 `PRAGMA busy_timeout` 永远看到 5000 —— 看起来完全正确。
只有**改配置再看**才会发现它纹丝不动。

所以这里不测「默认值对不对」，只测「改配置 → 行为变」。
默认值测试对这类 bug 是无效的，会给人虚假的安全感。
"""

from __future__ import annotations

import sqlite3

import pytest

from memory.config import Settings
from memory.services.sqlite_conn import DEFAULT_TIMEOUT_S, open_db


def _settings(tmp_path, **over) -> Settings:
    s = Settings()
    s.DATABASE_URL = f"sqlite:///{(tmp_path / 'x.db').as_posix()}"
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _busy_timeout(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA busy_timeout").fetchone()[0])


def _journal_mode(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


# ---------------- 核心：改配置 -> 行为必须变 ----------------

@pytest.mark.parametrize("ms", [1000, 10000, 30000])
def test_busy_timeout_follows_config(tmp_path, ms: int) -> None:
    """**这条是本 spec 的核心证据。**

    如果实现里写死 `timeout=5.0`（改之前的真实状态），
    `DB_BUSY_TIMEOUT_MS=10000` 这条会失败 —— 而观察默认值永远发现不了。
    """
    s = _settings(tmp_path, DB_BUSY_TIMEOUT_MS=ms)
    conn = open_db(tmp_path / "busy.db", s)
    try:
        assert _busy_timeout(conn) == ms, (
            f"配置 {ms}ms 但实际 {_busy_timeout(conn)}ms —— 配置没生效"
        )
    finally:
        conn.close()


def test_busy_timeout_zero_is_allowed(tmp_path) -> None:
    """0 = 立即返回 SQLITE_BUSY，不等待。是合法配置，不是「用默认值」。"""
    s = _settings(tmp_path, DB_BUSY_TIMEOUT_MS=0)
    conn = open_db(tmp_path / "zero.db", s)
    try:
        assert _busy_timeout(conn) == 0
    finally:
        conn.close()


def test_wal_follows_config(tmp_path) -> None:
    """`SQLITE_WAL=False` 时新库不能是 wal 模式。"""
    s = _settings(tmp_path, SQLITE_WAL=False)
    conn = open_db(tmp_path / "nowal.db", s)
    try:
        assert _journal_mode(conn) != "wal"
    finally:
        conn.close()


def test_wal_enabled_by_config(tmp_path) -> None:
    s = _settings(tmp_path, SQLITE_WAL=True)
    conn = open_db(tmp_path / "yeswal.db", s)
    try:
        assert _journal_mode(conn) == "wal"
    finally:
        conn.close()


# ---------------- 兜底：不传 settings 时保持历史行为 ----------------

def test_no_settings_uses_process_config(tmp_path) -> None:
    """不传 settings 时用**进程级配置**，不是硬编码默认值。

    这一点很关键：11 个服务都不会传 settings（它们只持有 db_path）。
    如果 helper 在这条路径上退回硬编码 5.0，那「配置不生效」只是从
    服务里搬到了 helper 里 —— 看起来改了，实际没改。
    """
    from memory.config import load_settings

    expected = load_settings().DB_BUSY_TIMEOUT_MS
    conn = open_db(tmp_path / "plain.db")
    try:
        assert _busy_timeout(conn) == expected, (
            "没走进程配置，配置改了还是不生效"
        )
    finally:
        conn.close()


def test_explicit_timeout_overrides_everything(tmp_path) -> None:
    s = _settings(tmp_path, DB_BUSY_TIMEOUT_MS=10000)
    conn = open_db(tmp_path / "over.db", s, timeout_s=0.25)
    try:
        assert _busy_timeout(conn) == 250
    finally:
        conn.close()


# ---------------- 真实并发：两个连接同时写不能立刻失败 ----------------

def test_concurrent_writers_wait_instead_of_failing(tmp_path) -> None:
    """一个连接持写锁时，另一个在 busy_timeout 内会**等**，不是立刻报错。

    这是 busy_timeout 的实际意义，也是「配置不生效」时用户能感受到的症状。
    """
    s = _settings(tmp_path, DB_BUSY_TIMEOUT_MS=2000)
    db = tmp_path / "conc.db"

    writer = open_db(db, s)
    try:
        writer.execute("CREATE TABLE t (id INTEGER)")
        writer.commit()
        # 拿排他锁但不提交
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO t VALUES (1)")

        other = open_db(db, s)
        try:
            # 应该等约 2 秒后抛 database is locked，而不是立刻抛
            import time

            t0 = time.monotonic()
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
            elapsed = time.monotonic() - t0
            assert elapsed >= 1.5, f"只等了 {elapsed:.2f}s —— busy_timeout 没生效"
        finally:
            other.close()
    finally:
        writer.rollback()
        writer.close()
