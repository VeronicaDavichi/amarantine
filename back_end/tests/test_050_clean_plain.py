"""spec 050 延伸：qq-mcp 解密副本的清理（用完就删）。"""

from __future__ import annotations

from pathlib import Path as _P

#: repo 根（tests → back_end → repo）。**不要写绝对路径**：
#: 本机是 /mnt/e/...，CI 是 /home/runner/work/...，写死会让 CI 直接失败。
REPO_ROOT = _P(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "harness"


import pathlib
import time

import pytest

import sys

sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "harness"))

from clean_qq_plain import candidates, clean  # noqa: E402


def _mk(dirp: pathlib.Path, name: str, size: int = 10) -> pathlib.Path:
    p = dirp / name
    p.write_bytes(b"x" * size)
    return p


@pytest.fixture()
def dirs(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    return plain, src


def test_only_plain_copies_selected(dirs) -> None:
    plain, _src = dirs
    _mk(plain, "nt_msg.db.100.1794.plain.db")
    _mk(plain, "nt_msg.db.100.1794.plain.db-wal")
    _mk(plain, "nt_msg.db.100.1794.plain.db-shm")
    _mk(plain, "qqkey")                     # 不能删
    _mk(plain, "随便一个文件.txt")            # 不能删
    got = [p.name for p in candidates(plain)]
    assert set(got) == {
        "nt_msg.db.100.1794.plain.db",
        "nt_msg.db.100.1794.plain.db-wal",
        "nt_msg.db.100.1794.plain.db-shm",
    }


def test_refuses_when_source_missing(dirs) -> None:
    """原库没了，副本就是唯一数据源 —— 拒绝删除。"""
    plain, _src = dirs
    _mk(plain, "nt_msg.db.100.1794.plain.db")
    rep = clean(plain_dir=plain, src_dir=plain.parent / "不存在", apply=True)
    assert rep["skipped_reason"]
    assert rep["files"] == []
    assert (plain / "nt_msg.db.100.1794.plain.db").exists(), "不该删任何文件"


def test_older_than_skips_fresh_files(dirs) -> None:
    plain, src = dirs
    old = _mk(plain, "old.db.1.1.plain.db")
    new = _mk(plain, "new.db.1.2.plain.db")
    past = time.time() - 3600
    import os

    os.utime(old, (past, past))
    got = [p.name for p in candidates(plain, older_than_s=600)]
    assert got == ["old.db.1.1.plain.db"]
    assert new.exists()


def test_apply_removes_and_reports_bytes(dirs) -> None:
    plain, src = dirs
    _mk(plain, "a.db.1.1.plain.db", size=100)
    _mk(plain, "b.db.1.2.plain.db", size=50)
    rep = clean(plain_dir=plain, src_dir=src, apply=True)
    assert len(rep["files"]) == 2
    assert rep["freed_bytes"] == 150
    assert list(plain.iterdir()) == []


def test_dry_run_keeps_files(dirs) -> None:
    plain, src = dirs
    _mk(plain, "a.db.1.1.plain.db")
    rep = clean(plain_dir=plain, src_dir=src, apply=False)
    assert rep["files"] and not rep.get("applied")
    assert list(plain.iterdir()), "预演不该删文件"


def test_missing_dir_is_noop(tmp_path) -> None:
    rep = clean(plain_dir=tmp_path / "不存在", src_dir=tmp_path, apply=True)
    assert rep["files"] == []
