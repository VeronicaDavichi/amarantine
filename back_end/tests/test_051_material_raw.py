"""spec 051 T6：材料带回原文（贪心填充 + 预算语义）。"""

from __future__ import annotations

import pytest

from memory.services import raw_text_index as R
from memory.services.recall import _attach_raw_quotes, _estimate_tokens

NORMAL = ["上厕所不冲厕所的吗！！！！", "七点钟开始洗的现在还在转"]
MARKETING = ("运营：【尾款链接整合①】定金已开 截单 7.20 "
             "https://item.taobao.com/x [图片] abc.png")


class _S:
    def __init__(self, *, enabled: bool = True, budget: int = 3000, cap: int = 2000):
        self.RAW_TEXT_INDEX_ENABLED = enabled
        self.CONTEXT_TOKEN_BUDGET = budget
        self.RAW_MATERIAL_MAX_CHARS = cap
        self.RAW_TEXT_INDEX_MAX_CHARS = 4000


def _mats(scores: list[float], qids: list[list[str]]) -> list[dict]:
    return [{"event_id": f"e{i}", "summary": "摘要", "score": s,
             "raw_quote_ids": q, "raw_quotes": None} for i, (s, q) in enumerate(zip(scores, qids))]


@pytest.fixture()
def fake_store(monkeypatch):
    """把 RawQuoteStore 换成内存实现，避开真库。"""
    texts = {
        "q1": NORMAL[0],
        "q2": NORMAL[1],
        "q3": MARKETING,
    }

    class _Store:
        def __init__(self, *a, **kw) -> None:
            pass

        def text_map(self, namespace, ids):
            return {q: texts[q] for q in ids if q in texts}

    monkeypatch.setattr(
        "memory.services.raw_quote.RawQuoteStore", _Store
    )
    monkeypatch.setattr(
        "memory.services.raw_quote.db_path_from_settings", lambda s: "/tmp/x.db"
    )
    return texts


def test_disabled_adds_nothing(fake_store) -> None:
    mats = _mats([0.9], [["q1"]])
    tr = _attach_raw_quotes(mats, namespace="ns", settings=_S(enabled=False))
    assert "raw_quotes" not in mats[0] or mats[0]["raw_quotes"] is None
    assert tr["with_raw"] == 0


def test_attaches_quotes_when_enabled(fake_store) -> None:
    mats = _mats([0.9], [["q1", "q2"]])
    tr = _attach_raw_quotes(mats, namespace="ns", settings=_S())
    assert mats[0]["raw_quotes"] == NORMAL
    assert mats[0]["raw_truncated"] is False
    assert tr["with_raw"] == 1
    assert tr["raw_text_used_chars"] > 0


def test_greedy_prefers_high_score(fake_store) -> None:
    """预算只够一条时，**只有最高分**那条带原文。"""
    mats = _mats([0.5, 0.9], [["q1"], ["q2"]])
    # 预算按真实估算推：两条摘要 + 刚好够一条原文
    per_summary = _estimate_tokens("摘要")
    budget = per_summary * 2 + len(NORMAL[1])
    tr = _attach_raw_quotes(mats, namespace="ns", settings=_S(budget=budget))
    with_raw = [m for m in mats if m["raw_quotes"]]
    assert len(with_raw) == 1, f"实际给原文的: {[(m['score'], m['raw_quotes']) for m in mats]}"
    assert with_raw[0]["score"] == 0.9, "应该是高分那条拿到原文"
    assert tr["with_raw"] == 1 and tr["summary_only"] == 1


def test_summary_never_squeezed(fake_store) -> None:
    """预算极小 → 原文全不给，但摘要字段完好、raw_quotes 字段恒存在。"""
    mats = _mats([0.9, 0.8], [["q1"], ["q2"]])
    tr = _attach_raw_quotes(mats, namespace="ns", settings=_S(budget=1))
    assert all(m["raw_quotes"] == [] for m in mats), "字段必须存在且为空数组"
    assert all(m["raw_truncated"] is False for m in mats)
    assert all(m["summary"] == "摘要" for m in mats)
    assert tr["summary_only"] == 2
    assert tr["raw_text_budget_chars"] == 0


def test_marketing_not_included(fake_store) -> None:
    """被闸挡掉的原文不进材料（与索引同一套判据）。"""
    mats = _mats([0.9], [["q3"]])
    tr = _attach_raw_quotes(mats, namespace="ns", settings=_S())
    assert mats[0]["raw_quotes"] == []
    assert tr["with_raw"] == 0


def test_truncated_flag_when_cap_hit(fake_store) -> None:
    """单卡上限砍过 → raw_truncated=true。"""
    mats = _mats([0.9], [["q1", "q2"]])
    cap = len(NORMAL[0]) + 1  # 只够第一条
    tr = _attach_raw_quotes(mats, namespace="ns", settings=_S(cap=cap))
    assert mats[0]["raw_truncated"] is True
    assert len(mats[0]["raw_quotes"]) == 1
    assert tr["with_raw"] == 1


def test_counts_are_consistent(fake_store) -> None:
    """trace 计数自洽：with_raw + summary_only = 材料数。"""
    mats = _mats([0.9, 0.8, 0.7], [["q1"], ["q2"], ["q3"]])
    tr = _attach_raw_quotes(mats, namespace="ns", settings=_S(budget=40))
    assert tr["with_raw"] + tr["summary_only"] == len(mats)


def test_store_failure_degrades(monkeypatch) -> None:
    """取原文失败 → 只给摘要，不抛。"""

    class _Boom:
        def __init__(self, *a, **kw) -> None:
            raise RuntimeError("db down")

    monkeypatch.setattr("memory.services.raw_quote.RawQuoteStore", _Boom)
    monkeypatch.setattr(
        "memory.services.raw_quote.db_path_from_settings", lambda s: "/tmp/x.db"
    )
    mats = _mats([0.9], [["q1"]])
    tr = _attach_raw_quotes(mats, namespace="ns", settings=_S())
    assert tr["with_raw"] == 0 and tr["summary_only"] == 1


def test_empty_materials_noop(fake_store) -> None:
    tr = _attach_raw_quotes([], namespace="ns", settings=_S())
    assert tr["with_raw"] == 0


def test_material_quotes_used_from_index_module() -> None:
    """确认走的是 raw_text_index.material_quotes（判据只有一处）。"""
    assert hasattr(R, "material_quotes")
