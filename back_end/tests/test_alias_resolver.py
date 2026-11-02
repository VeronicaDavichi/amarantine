"""LLM 推断别名：把描述解析成具体名字。

场景：`糖包就是那个AI里最弱智的`

规则层只能标 `uncertain` 且不建边 —— 右边不是名字。
要落到「豆包」，得先知道「最弱智的AI」是谁，这步只有 LLM 能做。

**但 LLM 只能做选择题**：给一份候选名单，它只能从里面挑。
自由生成的名字会变成新节点，错了就是永久污染。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from memory.config import Settings
from memory.models.event_card import (
    ALIAS_EXACT,
    ALIAS_PROBABLE,
    ALIAS_UNCERTAIN,
    AliasClaim,
)
from memory.services.alias_resolver import (
    MAX_CANDIDATES,
    _label_for,
    _parse_choice,
    resolve_claims,
    resolve_one,
)


def _settings() -> Settings:
    return Settings(
        MEMORY_NAMESPACE="work",
        WEAVIATE_URL="http://weaviate.test",
        NEO4J_URI="bolt://neo4j.test:7687",
    )


class FakeLLM:
    """按脚本返回内容的假 LLM。"""

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.system: list[str | None] = []

    def __call__(self, messages, settings, *, system=None, **kw):
        self.prompts.append(messages[0]["content"])
        self.system.append(system)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _claim(
    alias: str = "糖包",
    canonical: str = "那个AI里最弱智的",
    label: str = ALIAS_UNCERTAIN,
) -> AliasClaim:
    return AliasClaim(alias=alias, canonical=canonical, label=label)  # type: ignore[arg-type]


def _patch_chat(monkeypatch, fake: FakeLLM) -> None:
    import memory.services.alias_resolver as AR

    monkeypatch.setattr(AR, "chat", fake)


# ---------------- 正常解析 ----------------

def test_resolves_description_to_candidate(monkeypatch) -> None:
    """核心场景：`那个AI里最弱智的` → `豆包`。"""
    fake = FakeLLM('{"canonical": "豆包", "confidence": 0.85, "reason": "AI 产品里最弱的"}')
    _patch_chat(monkeypatch, fake)

    got = resolve_one(
        _claim(), ["豆包", "张三", "项目A"], settings=_settings()
    )

    assert got is not None
    assert got.alias == "糖包"
    assert got.canonical == "豆包"
    assert got.label == ALIAS_PROBABLE


def test_source_is_llm_inferred(monkeypatch) -> None:
    """**推断出来的永远不是 `user_declared`** —— 追溯时要能分开。

    混在一起之后就说不清「这条关系到底用户说的还是系统猜的」。
    """
    _patch_chat(monkeypatch, FakeLLM('{"canonical": "豆包", "confidence": 0.9}'))
    got = resolve_one(_claim(), ["豆包"], settings=_settings())
    assert got is not None
    assert got.source == "llm_inferred"


def test_confidence_below_probable_stays_uncertain(monkeypatch) -> None:
    """LLM 只敢给 0.6 —— 建边，但标 uncertain，不许伪装成事实。"""
    _patch_chat(monkeypatch, FakeLLM('{"canonical": "豆包", "confidence": 0.6}'))
    got = resolve_one(_claim(), ["豆包"], settings=_settings())
    assert got is not None
    assert got.label == ALIAS_UNCERTAIN


def test_too_low_confidence_discarded(monkeypatch) -> None:
    """0.3 连 uncertain 都不够，直接丢。"""
    _patch_chat(monkeypatch, FakeLLM('{"canonical": "豆包", "confidence": 0.3}'))
    assert resolve_one(_claim(), ["豆包"], settings=_settings()) is None


def test_never_upgrades_exact_via_llm(monkeypatch) -> None:
    """`exact` 是「用户自己说的」，LLM 再确定也不能给这一档。"""
    assert _label_for(1.0) == ALIAS_PROBABLE
    assert _label_for(0.99) == ALIAS_PROBABLE


# ---------------- 只做选择题 ----------------

def test_name_outside_candidates_rejected(monkeypatch) -> None:
    """LLM 编了一个候选外的名字 → 丢弃。

    不挡这一下，一句描述就能在图里凭空造出一个新节点。
    """
    _patch_chat(monkeypatch, FakeLLM('{"canonical": "通义千问", "confidence": 0.95}'))
    got = resolve_one(_claim(), ["豆包", "张三"], settings=_settings())
    assert got is None, "候选外的名字必须挡住"


def test_candidates_are_in_prompt(monkeypatch) -> None:
    fake = FakeLLM('{"canonical": "豆包", "confidence": 0.9}')
    _patch_chat(monkeypatch, fake)
    resolve_one(_claim(), ["豆包", "张三", "项目A"], settings=_settings())
    assert "豆包" in fake.prompts[0]
    assert "张三" in fake.prompts[0]


def test_self_is_excluded_from_candidates(monkeypatch) -> None:
    """`糖包` 自己不算候选 —— 否则 LLM 会说「糖包就是糖包」。"""
    fake = FakeLLM('{"canonical": "豆包", "confidence": 0.9}')
    _patch_chat(monkeypatch, fake)
    resolve_one(_claim(alias="糖包"), ["糖包", "豆包"], settings=_settings())
    # 提示词里左值会出现，但候选列表应该只剩豆包
    assert "已知对象：豆包" in fake.prompts[0]


def test_too_many_candidates_truncated(monkeypatch) -> None:
    fake = FakeLLM('{"canonical": "c0", "confidence": 0.9}')
    _patch_chat(monkeypatch, fake)
    many = [f"c{i}" for i in range(MAX_CANDIDATES + 20)]
    resolve_one(_claim(), many, settings=_settings())
    listed = fake.prompts[0].split("已知对象：")[1].split("\n")[0]
    assert listed.count("、") < MAX_CANDIDATES


# ---------------- 不该问 LLM 的情况 ----------------

def test_exact_claim_not_sent_to_llm(monkeypatch) -> None:
    """用户已经明说了，再问 LLM 是浪费且可能被改坏。"""
    fake = FakeLLM('{"canonical": "豆包", "confidence": 0.9}')
    _patch_chat(monkeypatch, fake)
    got = resolve_one(
        _claim(canonical="豆包", label=ALIAS_EXACT), ["豆包"], settings=_settings()
    )
    assert got is None
    assert fake.prompts == [], "exact 不该触发 LLM"


def test_probable_claim_not_re_resolved(monkeypatch) -> None:
    """已经推过一轮的不要再推 —— 否则会来回改。"""
    fake = FakeLLM('{"canonical": "豆包", "confidence": 0.9}')
    _patch_chat(monkeypatch, fake)
    got = resolve_one(
        _claim(canonical="豆包", label=ALIAS_PROBABLE), ["豆包"], settings=_settings()
    )
    assert got is None
    assert fake.prompts == []


def test_empty_candidates_short_circuits(monkeypatch) -> None:
    fake = FakeLLM('{"canonical": "豆包", "confidence": 0.9}')
    _patch_chat(monkeypatch, fake)
    assert resolve_one(_claim(), [], settings=_settings()) is None
    assert fake.prompts == [], "没候选就别白发请求"


def test_only_self_as_candidate_short_circuits(monkeypatch) -> None:
    fake = FakeLLM('{"canonical": "糖包", "confidence": 0.9}')
    _patch_chat(monkeypatch, fake)
    assert resolve_one(_claim(alias="糖包"), ["糖包"], settings=_settings()) is None


# ---------------- 容错 ----------------

def test_llm_error_returns_none(monkeypatch) -> None:
    """LLM 挂了不能拖垮调用方。"""
    from memory.services.llm import LLMError

    _patch_chat(monkeypatch, FakeLLM(LLMError("超时")))
    assert resolve_one(_claim(), ["豆包"], settings=_settings()) is None


def test_garbage_reply_returns_none(monkeypatch) -> None:
    _patch_chat(monkeypatch, FakeLLM("我觉得是豆包吧"))
    assert resolve_one(_claim(), ["豆包"], settings=_settings()) is None


def test_null_canonical_returns_none(monkeypatch) -> None:
    """LLM 老实说「选不出来」 —— 这是好行为，不是错误。"""
    _patch_chat(monkeypatch, FakeLLM('{"canonical": null, "confidence": 0.1}'))
    assert resolve_one(_claim(), ["豆包"], settings=_settings()) is None


@pytest.mark.parametrize("conf", ["abc", None, -1, 2.0])
def test_bad_confidence_clamped(monkeypatch, conf: Any) -> None:
    """离谱的置信度不能崩。2.0 夹到 1.0，-1 夹到 0。"""
    _patch_chat(
        monkeypatch, FakeLLM(json.dumps({"canonical": "豆包", "confidence": conf}))
    )
    got = resolve_one(_claim(), ["豆包"], settings=_settings())
    if conf == 2.0:
        assert got is not None and got.label == ALIAS_PROBABLE
    else:
        assert got is None


# ---------------- JSON 抠取 ----------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"canonical": "豆包"}', "豆包"),
        ('```json\n{"canonical": "豆包"}\n```', "豆包"),
        ('好的，答案是：{"canonical": "豆包"} 以上', "豆包"),
        ("not json at all", None),
        ("", None),
    ],
)
def test_parse_choice(raw: str, expected: Any) -> None:
    data = _parse_choice(raw)
    if expected is None:
        assert data is None
    else:
        assert data is not None and data["canonical"] == expected


# ---------------- 批量 ----------------

def test_resolve_claims_only_returns_new_ones(monkeypatch) -> None:
    """返回的是**新推断出来的**，原 claim 不出现在结果里。"""
    _patch_chat(monkeypatch, FakeLLM('{"canonical": "豆包", "confidence": 0.9}'))
    claims = [_claim(), _claim(alias="小张", canonical="那谁", label=ALIAS_EXACT)]
    got = resolve_claims(claims, candidates=["豆包"], settings=_settings())
    assert len(got) == 1
    assert got[0].alias == "糖包"


def test_resolve_claims_skips_non_uncertain(monkeypatch) -> None:
    fake = FakeLLM('{"canonical": "豆包", "confidence": 0.9}')
    _patch_chat(monkeypatch, fake)
    claims = [
        _claim(label=ALIAS_EXACT),
        _claim(label=ALIAS_PROBABLE),
    ]
    assert resolve_claims(claims, candidates=["豆包"], settings=_settings()) == []
    assert fake.prompts == []
