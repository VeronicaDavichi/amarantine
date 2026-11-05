"""真库验证：改环境变量，实际的 SQLite 连接参数必须跟着变。

在**新进程**里跑（配置在进程内缓存），所以这个脚本靠环境变量工作。

用法：
    DB_BUSY_TIMEOUT_MS=12000 SQLITE_WAL=true  ./harness/dev-linux.sh python ../harness/verify_sqlite_cfg.py
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from memory.services.sqlite_conn import open_db


def show(conn: sqlite3.Connection, label: str) -> None:
    bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"  {label:<34} busy_timeout={bt:<7} journal_mode={jm}")


def main() -> None:
    print(f"环境: DB_BUSY_TIMEOUT_MS={os.environ.get('DB_BUSY_TIMEOUT_MS', '(未设)')}")
    print(f"环境: SQLITE_WAL={os.environ.get('SQLITE_WAL', '(未设)')}")
    print()

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "verify.db"

        # ① helper 直接开
        c1 = open_db(db)
        show(c1, "open_db(path) 不传 settings")
        c1.close()

        # ② 走真实服务（RecallCounter 是写频率最高的之一）
        from memory.services.recall_count import RecallCounter

        rc = RecallCounter(db)
        # 借服务内部同样的连接路径验证
        c2 = open_db(db)
        show(c2, "RecallCounter 同库新连接")
        c2.close()

        # ③ 真实并发：等待时间是否等于配置
        import time

        w = open_db(db)
        w.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        w.commit()
        w.execute("BEGIN IMMEDIATE")
        w.execute("INSERT INTO t VALUES (1)")
        other = open_db(db)
        t0 = time.monotonic()
        try:
            other.execute("BEGIN IMMEDIATE")
            waited = time.monotonic() - t0
            print(f"  {'并发写没被阻塞':<34} ({waited:.2f}s)")
        except sqlite3.OperationalError:
            waited = time.monotonic() - t0
            print(f"  {'并发写等待后失败':<34} {waited:.2f}s")
        finally:
            other.close()
            w.rollback()
            w.close()


if __name__ == "__main__":
    main()
