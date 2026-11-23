"""spec 051 T4：`db_path_from_settings` 的相对路径解析（回归测试）。

踩过的现场：从仓库根跑 harness 脚本时，`sqlite:///./data/memory.db` 被按 CWD
解析成 `<repo>/data/memory.db`；文件不存在，**SQLite 静默建了个空库**，
表现为「原文一条都查不到」，不报任何错。
"""

from __future__ import annotations

from pathlib import Path

from memory.services.raw_quote import BACK_END_DIR, db_path_from_settings


class _S:
    def __init__(self, url: str) -> None:
        self.DATABASE_URL = url


def test_relative_resolved_against_back_end() -> None:
    got = db_path_from_settings(_S("sqlite:///./data/memory.db"))
    assert got == (BACK_END_DIR / "data" / "memory.db").resolve()


def test_absolute_untouched(tmp_path) -> None:
    target = tmp_path / "x.db"
    got = db_path_from_settings(_S(f"sqlite:///{target}"))
    assert got == target.resolve()


def test_four_slashes_absolute(tmp_path) -> None:
    target = tmp_path / "y.db"
    got = db_path_from_settings(_S(f"sqlite:////{str(target).lstrip('/')}"))
    assert got == target.resolve()


def test_non_sqlite_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="只支持 SQLite"):
        db_path_from_settings(_S("postgresql://localhost/db"))


def test_resolution_does_not_depend_on_cwd(tmp_path, monkeypatch) -> None:
    """换个工作目录结果必须一样 —— 这正是那个 bug 的本质。"""
    a = db_path_from_settings(_S("sqlite:///./data/memory.db"))
    monkeypatch.chdir(tmp_path)
    b = db_path_from_settings(_S("sqlite:///./data/memory.db"))
    assert a == b, "CWD 变了结果就变 = 会另建一个空库"
    assert isinstance(a, Path)
