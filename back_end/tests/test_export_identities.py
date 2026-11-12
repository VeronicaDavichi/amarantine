"""spec 047：从 group_info.db 导出身份（含 64023 头衔列）。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3

import pytest


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location(
        "export_identities",
        "/mnt/e/PycharmProjects/amarantine/harness/export_qq_identities.py",
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def _make_db(path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        'CREATE TABLE group_member3 ('
        '"60001" TEXT, "1000" TEXT, "1002" TEXT, "20002" TEXT, "64003" TEXT, "64023" TEXT)'
    )
    con.executemany(
        'INSERT INTO group_member3 VALUES (?,?,?,?,?,?)',
        [
            ("485490399", "u_1", "2742830098", "铭旌", "机仆的鱼", "机仆"),
            ("485490399", "u_2", "334176846", "Monn", "", "妈妈桑"),
            ("485490399", "u_3", "1944114167", "羽生玲央", "羽生玲央", ""),
            ("777", "u_9", "1", "别的群", "", "别的头衔"),
        ],
    )
    con.commit()
    con.close()


def test_export_titles_and_files(tmp_path, mod) -> None:
    db = tmp_path / "group_info.db"
    _make_db(db)
    out = tmp_path / "ids"
    stats = mod.export(db, out)

    assert stats == {"groups": 2, "members": 4, "titled": 3}
    data = json.loads((out / "group-485490399.json").read_text(encoding="utf-8"))
    assert data["groupQq"] == "485490399"
    by_uid = {m["uid"]: m for m in data["members"]}
    assert by_uid["u_1"]["title"] == "机仆"
    assert by_uid["u_1"]["card"] == "机仆的鱼"
    assert by_uid["u_2"]["title"] == "妈妈桑"
    assert by_uid["u_3"]["title"] == ""


def test_export_empty_group_ids_skipped(tmp_path, mod) -> None:
    db = tmp_path / "g.db"
    con = sqlite3.connect(db)
    con.execute(
        'CREATE TABLE group_member3 ('
        '"60001" TEXT, "1000" TEXT, "1002" TEXT, "20002" TEXT, "64003" TEXT, "64023" TEXT)'
    )
    con.execute("INSERT INTO group_member3 VALUES (NULL,'u','1','n','','')")
    con.commit()
    con.close()
    stats = mod.export(db, tmp_path / "out")
    assert stats["groups"] == 0
