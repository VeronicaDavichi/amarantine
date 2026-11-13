"""spec 048：外网开关（默认关）的行为。"""

from __future__ import annotations

import pytest

from memory.services import abbr as A


def test_disabled_sends_no_request(monkeypatch, tmp_path) -> None:
    """关闭时完全不发外部请求，返回空表。"""
    calls = []

    def fake_post(self, *a, **kw):
        calls.append(1)
        raise AssertionError("不应该发请求")

    import httpx
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    cache = A.AbbrCache(tmp_path / "c.db")
    assert A.guess_abbr("lysk", cache=cache, enabled=False) == []
    assert calls == []


def test_disabled_still_reads_cache(monkeypatch, tmp_path) -> None:
    """关闭时缓存仍可用（之前联网抓到过）。"""
    cache = A.AbbrCache(tmp_path / "c.db")
    cache.put("lysk", ["恋与深空"])
    assert A.guess_abbr("lysk", cache=cache, enabled=False) == ["恋与深空"]


def test_enabled_does_request(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_post(self, url, json=None, **kw):
        calls.append(json["text"])

        class R:
            status_code = 200

            def json(self):
                return [{"name": "lysk", "trans": ["恋与深空"]}]

        return R()

    import httpx
    monkeypatch.setattr(httpx.Client, "post", fake_post)
    cache = A.AbbrCache(tmp_path / "c.db")
    assert A.guess_abbr("lysk", cache=cache, enabled=True) == ["恋与深空"]
    assert calls == ["lysk"]


def test_default_reads_setting(monkeypatch, tmp_path) -> None:
    """enabled=None 时读配置；默认配置应为关。"""
    monkeypatch.setattr(A, "nbnhhsh_enabled", lambda: False)
    cache = A.AbbrCache(tmp_path / "c.db")
    assert A.guess_abbr("lysk", cache=cache) == []


def test_local_dict_works_without_api(monkeypatch, tmp_path) -> None:
    """外网关闭时，本地词典仍能解析出暖暖。"""
    cache = A.AbbrCache(tmp_path / "c.db")
    texts = ["nn 老爱出黑丝", "暖暖 系列玩家", "暖暖 又出周边"]
    got = A.resolve_abbr("nn", texts, cache=cache, local={"nn": ["暖暖"]}, enabled=False)
    assert got["picked"] == "暖暖"
