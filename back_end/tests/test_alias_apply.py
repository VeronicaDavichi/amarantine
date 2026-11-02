"""别名推断的编排：读卡 → LLM → 写回卡和图。

`alias_resolver` 测的是「一条 claim 能不能解析」，
这里测「从哪读、写到哪、失败了怎么办」。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from memory.config import Settings
from memory.models.event_card import AliasClaim
from memory.services.alias_apply import (
    ResolveReport,
    _load_claims,
    resolve_event_aliases,
    resolve_namespace_aliases,
)

GOOD_REPLY = '{"canonical": "豆包", "confidence": 0.9}'


def _settings() -> Settings:
    return Settings(
        MEMORY_NAMESPACE="work",
        WEAVIATE_URL="http://weaviate.test",
        NEO4J_URI="bolt://neo4j.test:7687",
    )


def _card(event_id: str = "evt_a", claims: list[dict] | None = None) -> dict:
    return {
        "event_id": event_id,
        "namespace": "work",
        "alias_claims": json.dumps(claims or [], ensure_ascii=False),
    }


UNCERTAIN = {
    "alias": "糖包",
    "canonical": "那个AI里最弱智的",
    "label": "uncertain",
    "source": "user_declared",
}


class FakeStore:
    def __init__(self, cards: dict[str, dict]) -> None:
        self.cards = cards
        self.appended: list[tuple[str, str, list[dict]]] = []

    def ensure_collection(self) -> None:
        pass

    def get_by_event_id(self, namespace: str, event_id: str):
        return self.cards.get(event_id)

    def list_all(self, namespace: str, *, limit: int = 1000):
        return list(self.cards.values())[:limit]

    def append_alias_claims(
        self, namespace: str, event_id: str, new_claims: list[dict]
    ) -> int:
        self.appended.append((namespace, event_id, new_claims))
        return len(new_claims)

    def close(self) -> None:
        pass


class FakeLLM:
    """替身：直接替换 `alias_resolver.chat`。

    真实 `chat()` 的 `client` 参数是 httpx.Client，不是回调 —— 所以这里
    不走那个注入点，而是把 LLM 边界整个换掉。
    """

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls = 0

    def __call__(self, messages, settings, *, system=None, **kw):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _patch(monkeypatch, *, candidates: list[str] | None = None) -> None:
    import memory.services.alias_apply as AA

    monkeypatch.setattr(
        AA,
        "list_known_names",
        lambda *a, **kw: candidates if candidates is not None else ["豆包", "张三"],
    )
    monkeypatch.setattr(AA, "append_alias_edges", lambda *a, **kw: 1)


def _llm(monkeypatch, reply: str | Exception = GOOD_REPLY) -> FakeLLM:
    """把 `alias_resolver.chat` 换掉 —— 那是真正的 LLM 边界。"""
    import memory.services.alias_resolver as AR

    fake = FakeLLM(reply)
    monkeypatch.setattr(AR, "chat", fake)
    return fake


# ---------------- 正常路径 ----------------

def test_resolves_uncertain_claim(monkeypatch) -> None:
    """核心路径：存疑 claim → LLM → 写卡 + 写图。"""
    _patch(monkeypatch)
    _llm(monkeypatch)
    store = FakeStore({"evt_a": _card(claims=[UNCERTAIN])})

    report = resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )

    assert report.error is None
    assert len(report.resolved) == 1
    assert report.resolved[0].canonical == "豆包"
    assert report.written_to_card == 1
    assert report.written_to_graph == 1
    assert store.appended, "应该把新 claim 追加到卡上"


def test_card_and_graph_both_written(monkeypatch) -> None:
    """卡上的新 claim 标 `llm_inferred`，跟用户声明的分得开。"""
    _patch(monkeypatch)
    _llm(monkeypatch)
    store = FakeStore({"evt_a": _card(claims=[UNCERTAIN])})
    resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    _, _, claims = store.appended[0]
    assert claims[0]["source"] == "llm_inferred"


def test_exact_claims_not_resolved(monkeypatch) -> None:
    """用户已明说的不用再问 —— 不浪费 LLM 调用。"""
    _patch(monkeypatch)
    fake = _llm(monkeypatch)
    store = FakeStore(
        {
            "evt_a": _card(
                claims=[
                    {
                        "alias": "小张",
                        "canonical": "张三",
                        "label": "exact",
                        "source": "user_declared",
                    }
                ]
            )
        }
    )
    report = resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert report.resolved == []
    assert store.appended == []
    assert fake.calls == 0


def test_card_without_claims_short_circuits(monkeypatch) -> None:
    _patch(monkeypatch)
    _llm(monkeypatch)
    store = FakeStore({"evt_a": _card(claims=[])})
    report = resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert report.scanned == 0
    assert store.appended == []


# ---------------- 容错 ----------------

def test_missing_card_reports_error(monkeypatch) -> None:
    _patch(monkeypatch)
    _llm(monkeypatch)
    store = FakeStore({})
    report = resolve_event_aliases(
        "work", "evt_missing", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert report.error == "事件卡不存在"


def test_no_candidates_skips_llm(monkeypatch) -> None:
    """图上没有候选对象 —— 问 LLM 也没用，跳过。"""
    _patch(monkeypatch, candidates=[])
    fake = _llm(monkeypatch)
    store = FakeStore({"evt_a": _card(claims=[UNCERTAIN])})
    report = resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert report.resolved == []
    assert store.appended == []
    assert fake.calls == 0


def test_store_exception_becomes_error_not_raise(monkeypatch) -> None:
    """推断失败不能拖垮调用方 —— 报告里带 error 就够。"""
    _patch(monkeypatch)
    _llm(monkeypatch)

    class Boom(FakeStore):
        def append_alias_claims(self, *a, **kw):
            raise RuntimeError("weaviate 挂了")

    store = Boom({"evt_a": _card(claims=[UNCERTAIN])})
    report = resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert report.error is not None
    assert "weaviate" in report.error


def test_llm_error_becomes_empty_result(monkeypatch) -> None:
    """LLM 挂了 → 没解析出来，但不是异常。"""
    from memory.services.llm import LLMError

    _patch(monkeypatch)
    _llm(monkeypatch, LLMError("超时"))
    store = FakeStore({"evt_a": _card(claims=[UNCERTAIN])})
    report = resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert report.error is None
    assert report.resolved == []


# ---------------- 解析坏数据 ----------------

def test_load_claims_tolerates_bad_json() -> None:
    assert _load_claims({"alias_claims": "not json"}) == []


def test_load_claims_skips_incomplete_items() -> None:
    raw = json.dumps(
        [{"alias": "a"}, {"alias": "b", "canonical": "c", "label": "exact"}]
    )
    claims = _load_claims({"alias_claims": raw})
    assert len(claims) == 1
    assert claims[0].alias == "b"


def test_load_claims_empty_is_empty() -> None:
    assert _load_claims({}) == []
    assert _load_claims({"alias_claims": ""}) == []


# ---------------- 命名空间扫描 ----------------

def test_namespace_scan_only_touches_uncertain(monkeypatch) -> None:
    """只扫有存疑 claim 的卡，别的一大堆卡不碰。"""
    _patch(monkeypatch)
    _llm(monkeypatch)
    store = FakeStore(
        {
            "evt_a": _card("evt_a", claims=[UNCERTAIN]),
            "evt_b": _card(
                "evt_b",
                claims=[
                    {
                        "alias": "小张",
                        "canonical": "张三",
                        "label": "exact",
                        "source": "user_declared",
                    }
                ],
            ),
            "evt_c": _card("evt_c", claims=[]),
        }
    )
    reports = resolve_namespace_aliases(
        "work", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert len(reports) == 1
    assert reports[0].event_id == "evt_a"


def test_namespace_scan_respects_limit(monkeypatch) -> None:
    _patch(monkeypatch)
    _llm(monkeypatch)
    cards = {f"evt_{i}": _card(f"evt_{i}", claims=[UNCERTAIN]) for i in range(5)}
    store = FakeStore(cards)
    reports = resolve_namespace_aliases(
        "work", settings=_settings(), store=store, limit=2  # type: ignore[arg-type]
    )
    assert len(reports) == 2


def test_namespace_scan_fetches_candidates_once(monkeypatch) -> None:
    """候选名单只取一次，不该每张卡查一遍图。"""
    import memory.services.alias_apply as AA

    calls: list[int] = []

    def _count(*a, **kw):
        calls.append(1)
        return ["豆包", "张三"]

    monkeypatch.setattr(AA, "list_known_names", _count)
    monkeypatch.setattr(AA, "append_alias_edges", lambda *a, **kw: 1)
    _llm(monkeypatch)

    cards = {f"evt_{i}": _card(f"evt_{i}", claims=[UNCERTAIN]) for i in range(3)}
    resolve_namespace_aliases(
        "work", settings=_settings(), store=FakeStore(cards)  # type: ignore[arg-type]
    )
    assert len(calls) == 1, f"候选查询了 {len(calls)} 次，应该只 1 次"


def test_report_to_dict() -> None:
    r = ResolveReport(
        event_id="evt_a",
        scanned=2,
        resolved=[
            AliasClaim(
                alias="糖包", canonical="豆包", label="probable", source="llm_inferred"
            )
        ],
        written_to_card=1,
        written_to_graph=1,
    )
    d = r.to_dict()
    assert d["event_id"] == "evt_a"
    assert d["resolved"][0]["canonical"] == "豆包"
    assert d["resolved"][0]["source"] == "llm_inferred"


# ---------------- 重复推断 ----------------

def test_second_run_skips_already_inferred(monkeypatch) -> None:
    """规则层的原 claim 永远停在 uncertain，所以每跑一次都会重推。

    不拦的话，第二次推出来的置信度可能不同（LLM 不确定），
    卡上就会长出 `uncertain` + `probable` 两条同名记录。
    """
    _patch(monkeypatch)
    fake = _llm(monkeypatch)
    store = FakeStore(
        {
            "evt_a": _card(
                claims=[
                    UNCERTAIN,
                    {
                        "alias": "糖包",
                        "canonical": "豆包",
                        "label": "probable",
                        "source": "llm_inferred",
                    },
                ]
            )
        }
    )
    report = resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert report.resolved == []
    assert store.appended == []
    assert fake.calls == 0, "已经推断过的不该再问 LLM"


def test_different_alias_still_resolved(monkeypatch) -> None:
    """推断过「糖包」，不代表「小张」也推断过了。"""
    _patch(monkeypatch)
    fake = _llm(monkeypatch)
    store = FakeStore(
        {
            "evt_a": _card(
                claims=[
                    UNCERTAIN,
                    {
                        "alias": "糖包",
                        "canonical": "豆包",
                        "label": "probable",
                        "source": "llm_inferred",
                    },
                    {
                        "alias": "小张",
                        "canonical": "那谁",
                        "label": "uncertain",
                        "source": "user_declared",
                    },
                ]
            )
        }
    )
    report = resolve_event_aliases(
        "work", "evt_a", settings=_settings(), store=store  # type: ignore[arg-type]
    )
    assert len(report.resolved) == 1
    assert report.resolved[0].alias == "小张"
    assert fake.calls == 1
