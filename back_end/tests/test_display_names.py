"""spec 047：群名片解析 / whois 查询（T2–T4）。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from memory.services import identity as I


def _import_chat():
    spec = importlib.util.spec_from_file_location(
        "import_chat_047", "/mnt/e/PycharmProjects/amarantine/harness/import_chat.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _write_identity(tmp_path: Path) -> Path:
    members = [
        {"uid": "u1", "qq": 1, "nickname": "潮间带", "card": "往汐（北瑀家的鱼）"},
        {"uid": "u2", "qq": 2, "nickname": "琴酒拜松年", "card": "往汐（北瑀家的鱼）复活版"},
        {"uid": "u3", "qq": 3, "nickname": "神代思想犯", "card": "合并转发存在广告,禁言15分钟"},
        {"uid": "u4", "qq": 4, "nickname": "阿秋", "card": "　"},
        {"uid": "u5", "qq": 5, "nickname": "幽霊", "card": "往汐\u202e"},
    ]
    (tmp_path / "group-485490399.json").write_text(
        json.dumps({"groupQq": "485490399", "members": members}, ensure_ascii=False),
        encoding="utf-8",
    )
    return tmp_path


def test_load_group_names_prefers_card_and_excludes_junk(tmp_path) -> None:
    mod = _import_chat()
    names, stats = mod.load_group_names(_write_identity(tmp_path), "qq-485490399")
    assert names["u1"] == "往汐（北瑀家的鱼）"
    assert names["u2"] == "往汐（北瑀家的鱼）复活版"
    # 处罚名片 → 回落昵称
    assert names["u3"] == "神代思想犯"
    # 全空白名片 → 回落昵称
    assert names["u4"] == "阿秋"
    # 伪装名 → 回落昵称（幽霊 可用）
    assert names["u5"] == "幽霊"
    assert stats["members"] == 5


def test_load_group_names_missing_file(tmp_path) -> None:
    mod = _import_chat()
    names, stats = mod.load_group_names(tmp_path, "qq-123456")
    assert names == {}
    assert stats["members"] == 0


def test_build_turns_resolves_sender_by_uid() -> None:
    """sender_uid → 群名片；没有 uid 时回落 sender_name/sender。"""
    mod = _import_chat()
    data = {
        "messages": [
            {"sender_uid": "u1", "sender": "505456846", "text": "蓝色键帽还有货吗各位", "ts": "2026-05-01 10:00:00"},
            {"sender_name": "已知昵称", "text": "有的有的，今天发货", "ts": "2026-05-01 10:01:00"},
        ]
    }
    turns, _ = mod.build_turns(data, uid_names={"u1": "往汐（北瑀家的鱼）"})
    assert turns[0]["text"].startswith("往汐（北瑀家的鱼）：")
    assert turns[1]["text"].startswith("已知昵称：")


# ------------------------------------------------------------ 图写入与查询


def test_display_name_cypher_uses_scope() -> None:
    assert "DISPLAYED_AS {scope: $scope}" in I.ENSURE_DISPLAY_NAME_CYPHER
    assert "MERGE (a:Account" in I.ENSURE_DISPLAY_NAME_CYPHER


def test_whois_cypher_filters_scope() -> None:
    assert "DISPLAYED_AS {scope: $scope}" in I.WHOIS_CYPHER
    assert "Alias {name: $name}" in I.WHOIS_CYPHER


class _Result:
    def single(self):
        return {"n": 2}


class _Session:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def run(self, cypher, **params):
        self.driver.calls.append((cypher, params))
        return _Result()


class _Driver:
    def __init__(self):
        self.calls = []

    def session(self):
        return _Session(self)

    def close(self):
        return None


def test_ensure_display_names_writes_items() -> None:
    drv = _Driver()
    stats = I.ensure_display_names(
        namespace="qq-505456846", scope="group:485490399",
        records=[{"uid": "u1", "name": "往汐（北瑀家的鱼）", "source": "group_card"}],
        settings=object(), driver=drv,
    )
    assert stats["written"] == 2
    _cypher, params = drv.calls[0]
    assert params["scope"] == "group:485490399"
    assert params["items"][0]["name"] == "往汐（北瑀家的鱼）"


def test_ensure_display_names_skips_empty() -> None:
    drv = _Driver()
    stats = I.ensure_display_names(
        namespace="x", scope="global",
        records=[{"uid": "", "name": "a"}, {"uid": "u1", "name": " "}],
        settings=object(), driver=drv,
    )
    assert stats["written"] == 0
    assert drv.calls == []
