import pathlib

p = pathlib.Path(
    "/mnt/e/PycharmProjects/amarantine/back_end/tests/test_sqlite_conn.py"
)
s = p.read_text()

old = '''def test_no_settings_keeps_default_timeout(tmp_path) -> None:
    """未迁移的调用点不传 settings，行为必须和改之前一样（5.0 秒）。"""
    conn = open_db(tmp_path / "plain.db")
    try:
        assert _busy_timeout(conn) == int(DEFAULT_TIMEOUT_S * 1000)
    finally:
        conn.close()'''
new = '''def test_no_settings_uses_process_config(tmp_path) -> None:
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
        conn.close()'''
assert old in s
p.write_text(s.replace(old, new, 1))
print("test_no_settings_uses_process_config")
