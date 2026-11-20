"""spec 050 延伸：「我是谁」（SELF_QQ → uid → 规范名）。"""

from __future__ import annotations

import json
import pathlib

import pytest

from memory.services import self_identity as S


class _Settings:
    def __init__(self, qq: str = "", ident: str = ""):
        self.SELF_QQ = qq
        self.IDENTITY_DIR = ident


@pytest.fixture()
def ident_dir(tmp_path):
    d = tmp_path / "ids"
    d.mkdir()
    # 两个群，同一个 qq 在不同群有不同名片，昵称一致
    for gid, card in ((111, "群里的我"), (222, "另一个名片")):
        (d / f"group-{gid}.json").write_text(json.dumps({
            "groupQq": gid,
            "members": [
                {"uid": "u_me", "qq": 10000000001, "nickname": "Veronica Davichi",
                 "card": card},
                {"uid": "u_other", "qq": 999, "nickname": "别人", "card": "别人"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
    return d


def test_unconfigured(ident_dir) -> None:
    got = S.resolve_self(_Settings("", str(ident_dir)))
    assert got["configured"] is False
    assert "SELF_QQ" in got["reason"]
    assert got["uid"] == ""


def test_resolves_uid_and_names(ident_dir) -> None:
    got = S.resolve_self(_Settings("10000000001", str(ident_dir)))
    assert got["uid"] == "u_me"
    assert set(got["names"]) == {"群里的我", "另一个名片"}
    assert got["namespaces_seen"] == ["111", "222"]


def test_canonical_is_nickname_not_card(ident_dir) -> None:
    """规范名取**账号昵称**，不是某个群名片（实测 20 个名片里挑错成「海嗣」）。"""
    got = S.resolve_self(_Settings("10000000001", str(ident_dir)))
    assert got["canonical"] == "Veronica Davichi"


def test_missing_qq(ident_dir) -> None:
    got = S.resolve_self(_Settings("123456", str(ident_dir)))
    assert got["uid"] == ""
    assert "没有" in got["reason"]


def test_missing_dir(tmp_path) -> None:
    got = S.resolve_self(_Settings("10000000001", str(tmp_path / "无")))
    assert got["uid"] == ""
    assert "成员表目录不存在" in got["reason"]


def test_self_uids(ident_dir) -> None:
    assert S.self_uids(_Settings("10000000001", str(ident_dir))) == {"u_me"}


@pytest.mark.parametrize("names,expect", [
    ([".........大........群......呼....唤..........我........", "海嗣"], "海嗣"),
    (["Veronica Davichi", "苏苏大可爱"], "苏苏大可爱"),  # 同分取较短
    ([], ""),
])
def test_preferred_name(names, expect) -> None:
    assert S.preferred_name(names) == expect
