"""020：单字名前缀保护 —— 挡住「张三 / 张三丰」，放行「豆包 / 豆包AI」。

这个文件的核心不是在测代码，是在测**一个设计判断**：

编辑距离在这两类上是反向的（同人 0.50 < 不同人 0.67），所以规则不能基于距离，
只能基于中文人名结构。测试里保留了那组反向数据，防止将来有人「优化」成距离阈值。
"""

from __future__ import annotations

import pytest

from memory.services.alias_resolver import (
    PREFIX_CONFLICT_REASON,
    single_char_name_prefix_conflict,
)
from memory.services.labeler import split_name_surname


# ---------------- 结构判定 ----------------

@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("张三", "张"),        # 单姓 + 单字名
        ("王伟", "王"),
        ("欧阳锋", "欧阳"),    # 复姓 + 单字名
        ("司马懿", "司马"),
        ("李小狼", None),      # 单姓 + **双**字名
        ("豆包", None),        # `豆` 不是姓
        ("张三丰", None),      # 3 字但「张三」不是复姓
        ("项目A", None),
        ("AI", None),
    ],
)
def test_split_name_surname(word: str, expected: str | None) -> None:
    assert split_name_surname(word) == expected


# ---------------- 前缀冲突 ----------------

@pytest.mark.parametrize(
    ("short", "long"),
    [
        ("张三", "张三丰"),
        ("欧阳锋", "欧阳锋儿"),
        ("王伟", "王伟伟"),
    ],
)
def test_conflict_detected(short: str, long: str) -> None:
    assert single_char_name_prefix_conflict(short, long) is True


@pytest.mark.parametrize(
    ("short", "long", "why"),
    [
        ("豆包", "豆包AI", "`豆` 不是姓 —— 同人，必须放行"),
        ("李小狼", "小狼", "不是前缀关系"),
        ("张三", "李四", "不是前缀关系"),
        ("张三", "张三丰丰", "长度差 2，有昵称歧义，保守放行"),
        ("微信", "WeChat", "跨语言别名，字面完全无关"),
        ("项目A", "项目A组", "`项` 不是姓"),
    ],
)
def test_no_conflict(short: str, long: str, why: str) -> None:
    assert single_char_name_prefix_conflict(short, long) is False, why


def test_same_name_is_not_a_conflict() -> None:
    """自己和自己不该触发（虽然调用方已经排除，纯函数也要守住）。"""
    assert single_char_name_prefix_conflict("张三", "张三") is False


# ---------------- 为什么不能用编辑距离（钉住这个结论） ----------------

def _edit_distance(a: str, b: str) -> int:
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cur[j] = min(
                prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1])
            )
        prev = cur
    return prev[n]


def _similarity(a: str, b: str) -> float:
    longest = max(len(a), len(b))
    return 1.0 - _edit_distance(a, b) / longest if longest else 1.0


def test_edit_distance_is_reversed_for_these_pairs() -> None:
    """**这条是防回退的。**

    「豆包/豆包AI」是同人、「张三/张三丰」是不同人，
    但后者的字面相似度**更高**（0.67 > 0.50）。

    所以任何「相似度低于阈值就判不同实体」的做法都是反向的。
    如果将来有人把规则改回距离阈值，这条测试会解释为什么不行。
    """
    same_person = _similarity("豆包", "豆包AI")
    different_person = _similarity("张三", "张三丰")
    assert different_person > same_person, (
        f"前提变了：同人 {same_person:.2f} vs 不同人 {different_person:.2f}。"
        "如果这组数据变了，才值得重新讨论距离阈值方案。"
    )


# ---------------- resolve_one 的早退 ----------------

class _FakeLLM:
    """记录是否真的被调用，并返回一个**在候选里**的名字。

    注意：`resolve_one` 有白名单校验，返回候选外的名字会被丢弃 ——
    假 LLM 必须返回候选之一，否则测试会把「白名单丢弃」误判成「规则挡住了」。
    """

    def __init__(self, pick: str = "豆包AI") -> None:
        self.calls = 0
        self.pick = pick

    def __call__(self, prompt, *a, **kw):
        self.calls += 1
        return f'{{"canonical": "{self.pick}", "confidence": 0.9, "reason": "像"}}'


def _claim(alias: str, canonical: str = "张三"):
    from memory.models.event_card import AliasClaim

    return AliasClaim(
        alias=alias,
        canonical=canonical,
        label="uncertain",
        source="llm_inferred",
    )


def test_resolve_one_skips_llm_on_conflict(monkeypatch) -> None:
    """命中前缀冲突时**不调 LLM** —— 省一次调用，也不给它误判的机会。

    注意 patch 目标：`alias_resolver` 是 `from ...llm import chat`，
    `chat` 这个名字绑在**它自己的模块命名空间**里。
    patch `llm_mod.chat` 对它无效（这是个会让测试假通过的坑）。
    """
    from memory.services import alias_resolver

    fake = _FakeLLM()
    monkeypatch.setattr(alias_resolver, "chat", fake)

    from memory.config import load_settings

    got = alias_resolver.resolve_one(
        _claim("张三"), ["张三丰", "李四"], settings=load_settings()
    )
    assert got is None, "应该判为新实体，不该返回合并结果"
    assert fake.calls == 0, f"LLM 被调用了 {fake.calls} 次，早退没生效"


def test_resolve_one_bidirectional(monkeypatch) -> None:
    """alias 是长的那个（张三丰）也要挡住。"""
    from memory.services import alias_resolver
    from memory.config import load_settings

    fake = _FakeLLM()
    monkeypatch.setattr(alias_resolver, "chat", fake)

    got = alias_resolver.resolve_one(
        _claim("张三丰"), ["张三"], settings=load_settings()
    )
    assert got is None
    assert fake.calls == 0


def test_resolve_one_still_calls_llm_when_no_conflict(monkeypatch) -> None:
    """不冲突时照常走 LLM —— 这条防止「规则太宽把所有候选都挡掉」。"""
    from memory.services import alias_resolver
    from memory.config import load_settings

    fake = _FakeLLM()
    monkeypatch.setattr(alias_resolver, "chat", fake)

    got = alias_resolver.resolve_one(
        _claim("豆包"), ["豆包AI"], settings=load_settings()
    )
    assert fake.calls == 1, "不该挡住同人别名"
    assert got is not None


def test_reason_constant_is_used_in_log(caplog, monkeypatch) -> None:
    """挡住时要留下可 grep 的日志（留痕不完整，但至少可见）。"""
    import logging

    from memory.config import load_settings
    from memory.services import alias_resolver

    monkeypatch.setattr(alias_resolver, "chat", _FakeLLM())

    with caplog.at_level(logging.INFO, logger="memory.services.alias_resolver"):
        alias_resolver.resolve_one(
            _claim("张三"), ["张三丰"], settings=load_settings()
        )
    assert PREFIX_CONFLICT_REASON in caplog.text
