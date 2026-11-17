"""spec 016 T5：candidate_alias 人工确认池。"""

from __future__ import annotations

from memory.services.alias_resolver import AliasClaim, resolve_one


class _Settings:
    SINGLE_CHAR_PREFIX_GUARD = True


def _claim(alias: str, canonical: str) -> AliasClaim:
    return AliasClaim(alias=alias, canonical=canonical, label="uncertain",
                      source="llm_inferred")


def test_prefix_conflict_goes_to_pool() -> None:
    """单字名前缀冲突 → 不合并，但进 candidate 池（以前只写日志）。"""
    declined: list[dict] = []
    got = resolve_one(
        _claim("张三丰", "张三"),
        ["张三", "李四"],
        settings=_Settings(),
        declined=declined,
    )
    assert got is None
    assert declined, "冲突必须进池子，否则人永远不知道机器看过"
    assert declined[0]["reason"] == "single_char_prefix"
    assert declined[0]["alias"] == "张三丰"


def test_no_declined_when_not_passing_list() -> None:
    """不传 declined 时行为不变（老调用不受影响）。"""
    got = resolve_one(_claim("张三丰", "张三"), ["张三"], settings=_Settings())
    assert got is None


def test_resolved_does_not_enter_pool() -> None:
    """合并成功的不该进池子。"""
    declined: list[dict] = []
    resolve_one(_claim("豆包AI", "豆包"), ["豆包"], settings=_Settings(),
                declined=declined)
    # 规则层能判定同一实体时 declined 应为空（无论是否命中）
    assert all(d["alias"] != "豆包AI" or d["reason"] != "single_char_prefix"
               for d in declined)
