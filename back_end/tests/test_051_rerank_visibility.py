"""rerank 静默失效修复（spec 051 后续）：413 特判、结果可见、配置自检。"""

from __future__ import annotations

import re

import pytest

from memory.services import rerank as RR


class _S:
    RERANK_ENABLED = True
    RERANK_MODEL = "bge-reranker-v2-m3"
    RERANK_BASE_URL = "http://127.0.0.1:8083"
    RERANK_TIMEOUT_MS = 800
    RERANK_CANDIDATES = 50
    RECALL_BUDGET_MS = 1500
    RERANK_API_KEY = ""


class _Resp:
    def __init__(self, status: int, body: str = "", payload=None) -> None:
        self.status_code = status
        self.text = body
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, resp) -> None:
        self._resp = resp
        self.calls = 0

    def post(self, *a, **kw):
        self.calls += 1
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_tracker(monkeypatch):
    monkeypatch.setattr(RR, "_LAST", {"ok": None, "reason": "", "elapsed_ms": 0})
    monkeypatch.setattr(RR, "_CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(RR, "_louded", False)


def test_413_batch_limit_translated() -> None:
    """413 + batch size → 专用 reason（不是含糊的 http_413）。"""
    body = '{"error":"batch size 50 > maximum allowed batch size 4"}'
    res = RR.rerank("q", [("e1", "t")], settings=_S(),
                    client=_Client(_Resp(413, body)))
    assert res.skipped is True
    assert res.reason == "http_413_batch_limit"


def test_413_other_reason_kept() -> None:
    """413 但不是 batch 问题 → 保留原样，不误报。"""
    res = RR.rerank("q", [("e1", "t")], settings=_S(),
                    client=_Client(_Resp(413, '{"error":"payload too large"}')))
    assert res.reason == "http_413"


def test_success_recorded() -> None:
    res = RR.rerank("q", [("e1", "t"), ("e2", "u")], settings=_S(),
                    client=_Client(_Resp(200, payload=[
                        {"index": 0, "score": 0.9}, {"index": 1, "score": 0.2}])))
    assert res.skipped is False
    got = RR.last_outcome()
    assert got["ok"] is True
    assert got["consecutive_failures"] == 0


def test_failure_counted_and_reset() -> None:
    """连续失败累加；一旦成功就归零。"""
    bad = _Client(RuntimeError("boom"))
    for _ in range(3):
        RR.rerank("q", [("e1", "t")], settings=_S(), client=bad)
    assert RR.last_outcome()["consecutive_failures"] == 3
    assert RR.last_outcome()["ok"] is False

    good = _Client(_Resp(200, payload=[{"index": 0, "score": 0.5}]))
    RR.rerank("q", [("e1", "t")], settings=_S(), client=good)
    assert RR.last_outcome()["consecutive_failures"] == 0
    assert RR.last_outcome()["ok"] is True


def test_disabled_not_tracked() -> None:
    class _Off(_S):
        RERANK_ENABLED = False

    res = RR.rerank("q", [("e1", "t")], settings=_Off(), client=_Client(_Resp(200)))
    assert res.reason == "disabled"
    assert RR.last_outcome()["ok"] is None, "关掉时不该污染追踪状态"


# ---------------- 启动自检 ----------------

def test_startup_check_flags_infeasible_pool() -> None:
    """50 条 × 120ms 远超 800ms → 必须告警，并给出可行的条数建议。"""
    warns = RR.startup_check(_S())
    assert warns, "这种配置必须被指出来"
    msg = next(w for w in warns if "RERANK_CANDIDATES" in w)

    # 建议条数必须真的比当前小、且为正（不依赖具体数字，避免测试跟着公式漂移）
    m = re.search(r"降到 (\d+) 以内", msg)
    assert m, f"应给出具体建议条数: {msg}"
    suggested = int(m.group(1))
    assert 0 < suggested < _S.RERANK_CANDIDATES
    # 建议值必须真的能在预算内跑完（用公式反推，误差容许 1）
    assert suggested * 120 + 130 <= _S.RERANK_TIMEOUT_MS + 120


def test_startup_check_quiet_when_small_pool() -> None:
    class _Small(_S):
        RERANK_CANDIDATES = 4

    assert RR.startup_check(_Small()) == []


def test_startup_check_quiet_when_disabled() -> None:
    class _Off(_S):
        RERANK_ENABLED = False

    assert RR.startup_check(_Off()) == []


def test_startup_check_flags_recall_budget_overrun() -> None:
    class _Big(_S):
        RERANK_CANDIDATES = 100
        RERANK_TIMEOUT_MS = 60000  # 超时够大，但会挤爆召回预算

    warns = RR.startup_check(_Big())
    assert any("RECALL_BUDGET_MS" in w for w in warns)
