"""SQLite 连接的统一入口。

## 为什么需要它

原来 11 个服务各自写 `sqlite3.connect(path, timeout=5.0)`，其中 5 个写 `10`，
没有任何理由说明为什么分两组。更关键的是：**这两个值都不是配置**，
`DB_BUSY_TIMEOUT_MS` 完全没有代码读它。

它曾经「看起来生效」好几个月，因为 `5000ms` 恰好等于 Python
`sqlite3.connect()` 的 `timeout=5.0` 默认值。观察 `PRAGMA busy_timeout`
永远看到 5000，只有把配置改成 10000 再看才会发现它纹丝不动。

所以这个模块的价值不只是「少写几行」：它让并发参数**只剩一个可配置来源**。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from memory.config import Settings

logger = logging.getLogger(__name__)

#: 没有 settings 时的兜底等待时间（秒）。
#: 等于历史上的硬编码值，保证未迁移的调用点行为不变。
DEFAULT_TIMEOUT_S = 5.0

#: 进程内缓存的配置。为什么可以缓存：配置在进程生命周期内不变
#: （改 .env 要重启），而每开一个连接就构造一次 Settings 太贵。
_default_settings: Settings | None = None
_settings_load_failed = False


def _process_settings() -> Settings | None:
    """懒加载一次进程级配置，供不传 settings 的调用点使用。

    失败时返回 None（退回 DEFAULT_TIMEOUT_S）而不是抛 ——
    一个连配置都加载不起来的进程，不该在开 SQLite 连接时炸掉，
    更该由上层入口（MCP / admin）明确报错退出。
    """
    global _default_settings, _settings_load_failed
    if _default_settings is not None:
        return _default_settings
    if _settings_load_failed:
        return None
    try:
        from memory.config import load_settings

        _default_settings = load_settings()
        return _default_settings
    except Exception as exc:  # noqa: BLE001
        logger.warning("加载配置失败，SQLite 将用兜底并发参数: %s", exc)
        _settings_load_failed = True
        return None


def open_db(
    path: str | Path,
    settings: Settings | None = None,
    *,
    timeout_s: float | None = None,
) -> sqlite3.Connection:
    """建一个按配置设好并发参数的 SQLite 连接。

    Args:
        path: 数据库文件路径。
        settings: 配置。为 None 时用 `DEFAULT_TIMEOUT_S` + WAL（历史行为）。
        timeout_s: 显式覆盖等待时间（测试用）。优先于 settings。

    Returns:
        已设好 `busy_timeout` 和（按配置）`journal_mode=WAL` 的连接。

    注意 `journal_mode` 是**持久化**属性（写在库文件里），
    而 `busy_timeout` 是**连接级**属性。所以 WAL 只在第一次生效，
    之后的连接读到的已经是 `wal` —— 设 `SQLITE_WAL=False` 也不会切回去。
    要让已有的库退出 WAL 必须显式 `PRAGMA journal_mode=DELETE`。
    """
    # 没显式给 settings 时用进程级配置，而不是硬编码默认值 ——
    # 否则「11 个服务各自写 timeout=5.0」的老问题只是从服务里搬到了这里。
    if settings is None:
        settings = _process_settings()

    if timeout_s is not None:
        timeout = float(timeout_s)
    elif settings is not None:
        timeout = max(0.0, float(settings.DB_BUSY_TIMEOUT_MS) / 1000.0)
    else:
        timeout = DEFAULT_TIMEOUT_S

    conn = sqlite3.connect(str(path), timeout=timeout)

    # WAL 是库级持久属性，但显式设一遍更安全：
    # 新库第一次连接时如果不设，就是默认的 delete 模式。
    wal = settings.SQLITE_WAL if settings is not None else True
    if wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:  # noqa: BLE001 - 设不上不该让连接失败
            logger.warning("设置 WAL 失败（继续用默认模式）: %s", exc)

    # `timeout` 参数已经等价于设 busy_timeout，但显式再设一遍：
    # 如果将来某处传了 timeout 又被别处覆盖，PRAGMA 才是权威值。
    # 而且它让「配置到底生没生效」可以直接观察（见测试）。
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")

    return conn
