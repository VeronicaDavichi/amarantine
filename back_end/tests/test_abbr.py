"""spec 048：缩写解析（nbnhhsh 候选 + 本地共现消歧）。"""

from __future__ import annotations

import json

import pytest

from memory.services import abbr as A


@pytest.fixture()
def cache(tmp_path):
    return A.AbbrCache(tmp_path / "cache.db")


def _local():
    return {"nn": ["暖暖"], "lysk": ["恋与深空"], "mz": ["母猪"]}


def test_scan_tokens_counts_only_standalone() -> None:
    texts = ["之前nn老爱出黑丝", "lysk 不是还出项圈", "annn 不算", "ABCdef"]
    counter = A.scan_abbr_tokens(texts)
    assert counter["nn"] == 1          # 「annn」里的 nn 前后是字母，不计数
    assert counter["lysk"] == 1
    assert counter["annn"] == 1
    assert "abcdef" in counter


def test_score_prefers_cooccurring_candidate() -> None:
    texts = [
        "之前 nn 老爱出黑丝啥的，lysk 不是还出项圈周边",
        "恋与深空 的周边确实好看，lysk 又出了",
        "恋与深空 的街道在 CP 上有",
    ]
    scored = A.score_candidates("lysk", ["恋与深空", "老姚烧烤"], texts)
    assert scored[0][0] == "恋与深空"
    assert scored[0][1] > scored[1][1]


def test_resolve_picks_clear_winner(cache) -> None:
    texts = [
        "lysk 不是还出项圈周边",
        "恋与深空 又出周边了",
        "恋与深空 福州 CP 有街道",
    ]
    got = A.resolve_abbr("lysk", texts, cache=cache, local=_local())
    assert got["picked"] == "恋与深空"
    assert got["ambiguous"] is False


def test_resolve_marks_ambiguous_when_tied(cache) -> None:
    """两候选分不出高下 → 不建边。"""
    texts = ["dz 在这里"]
    got = A.resolve_abbr("dz", texts, cache=cache, local={})
    assert got["picked"] is None
    assert got["ambiguous"] is True


def test_local_dict_supplies_missing_candidate(cache) -> None:
    """nbnhhsh 没有 nn=暖暖，靠本地词典补。"""
    texts = ["nn 老爱出黑丝", "暖暖 系列玩家", "暖暖 又出周边"]
    got = A.resolve_abbr("nn", texts, cache=cache, local=_local())
    assert any(c == "暖暖" for c, _ in got["candidates"])
    assert got["picked"] == "暖暖"


def test_guess_abbr_uses_cache(monkeypatch, cache) -> None:
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
    a = A.guess_abbr("lysk", cache=cache)
    b = A.guess_abbr("lysk", cache=cache)
    assert a == ["恋与深空"] == b
    assert calls == ["lysk"]  # 第二次走缓存


def test_guess_abbr_failure_returns_empty(monkeypatch, cache) -> None:
    import httpx

    def boom(self, *a, **kw):
        raise httpx.ConnectError("no net")

    monkeypatch.setattr(httpx.Client, "post", boom)
    assert A.guess_abbr("whatever", cache=cache) == []


def test_load_local_dict_reads_config() -> None:
    local = A.load_local_dict()
    assert local.get("lysk") == ["恋与深空"]
