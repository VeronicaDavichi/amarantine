"""spec 026：实体同一性的确定性规则层（ADR C.B3 的 ② 级）。

这个文件的核心任务是钉住**规则顺序**和**规则的能力边界**。

规则顺序错一次的后果是实测过的：
`张三哥/张三` 若先被「单字前缀冲突」判掉，称呼剥离永远没机会执行。
规则边界错一次的后果是「项目A/项目A组」被误并成同一个实体。

## 为什么这里完全没有向量

ADR 原方案是 embedding 余弦 >= 0.92。实测（bge-m3，14 组中文对照）：
    应合并最低   0.4806  周会 / 每周例会
    不该合并最高 0.8801  项目A / 项目A组
区间完全重叠，任何单一阈值都同时误并和漏并。详见 spec 026 Overview。
"""

from __future__ import annotations

import pytest

from memory.config import Settings
from memory.services.entity_match import (
    RULING_DIFFERENT,
    RULING_SAME,
    RULING_UNKNOWN,
    best_candidate,
    match_entities,
    single_char_name_prefix_conflict,
)


def _s(**over) -> Settings:
    s = Settings()
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _ruling(a: str, b: str, **over) -> str:
    return match_entities(a, b, settings=_s(**over)).ruling


# ============================================================ 规则 1：单字名前缀


@pytest.mark.parametrize("short,long", [("张三", "张三丰"), ("李四", "李四光")])
def test_single_char_prefix_is_different(short: str, long: str) -> None:
    """「姓+单字名」多一个字 → 不同实体。这条是 spec 020 的既有能力。"""
    assert _ruling(short, long) == RULING_DIFFERENT
    assert _ruling(long, short) == RULING_DIFFERENT, "必须双向"


def test_prefix_rule_survives_rule_disable() -> None:
    """关掉规则层时，单字前缀判定仍然生效。

    它属于 spec 020 的独立功能，不是这次新增 —— 关规则层不该把它一起关掉，
    否则会变成「回退到比改动前更差」。
    """
    assert _ruling("张三", "张三丰", ENTITY_RULE_ENABLED=False) == RULING_DIFFERENT


def test_prefix_rule_beats_suffix_whitelist() -> None:
    """**顺序测试**：默认配置下，`张三 / 张三丰` 仍是不同实体。

    ⚠️ **这个测试在 spec 031 被改过**，改的不是断言宽松，是场景。

    **原来测的是**：人为把「丰」塞进 `ENTITY_SUFFIXES`，断言仍然 DIFFERENT。
    当时的设计是「姓名结构 > 配置」—— 硬保护，防止用户误配。

    **031 修了另一个 bug 之后这条不再成立**：`上海/上海市` 之前被判成
    DIFFERENT，因为规则 1（单字名前缀冲突）抢在后缀规则之前，而 `上`
    恰好在姓氏表里。修法是让规则 1「多出的字在已知后缀表里时弃权」。
    于是人为配了「丰」之后，「张三丰」的「丰」也算已知后缀 → 合并。

    **这个权衡是有意接受的**：

    - `ENTITY_SUFFIXES` 的默认值只含「不可能是人名字」的字
      （`市省县区镇部处科院局`）和多字词（`AI/助手/公司/集团/科技/股份/...`）
    - 硬把「丰」这种**名字用字**配进去，等于告诉系统「丰是后缀」，系统照做
    - 想同时「保护姓名结构」又允许配任意字，规则层做不到 —— 那是 LLM 的活，
      三态设计里「不知道」就该交给下游

    所以这条现在验证**默认配置**下的行为，另外加一条记录这个已知权衡。
    """
    got = _ruling("张三", "张三丰")  # 用默认 ENTITY_SUFFIXES
    assert got == RULING_DIFFERENT, "默认配置下姓名结构判断被后缀规则抢先了"


def test_known_tradeoff_configuring_name_char_as_suffix_merges() -> None:
    """**已知权衡，不是 bug**：把名字用字配进后缀白名单会合并。

    这条测试存在的意义是**把权衡钉在代码里**，而不是让它变成
    某天有人发现时的「意外行为」。

    spec 031 让规则 1 对「已知后缀」弃权，代价就是这个：
    配置能覆盖姓名结构判断。换来的是 `上海/上海市` 这类地名不再被拆成
    两个实体 —— 后者在实际数据里远比「用户乱配后缀」常见。

    要彻底两者兼顾，需要「后缀字 vs 名字字」的语义区分，
    那是 LLM 层的事，规则层做不到。
    """
    got = _ruling("张三", "张三丰", ENTITY_SUFFIXES="丰,AI,公司")
    assert got == RULING_SAME, (
        "如果这里变成 DIFFERENT，说明规则 1 又开始抢后缀规则了 —— "
        "那 `上海/上海市` 的 bug 会回来"
    )


# ============================================================ 规则 2：称呼剥离


@pytest.mark.parametrize(
    "alias,canonical",
    [("张三哥", "张三"), ("张三经理", "张三"), ("张三老师", "张三"), ("李四同学", "李四")],
)
def test_honorific_strip_is_same(alias: str, canonical: str) -> None:
    assert _ruling(alias, canonical) == RULING_SAME, f"{alias} / {canonical}"


def test_honorific_single_char_result_is_unknown() -> None:
    """`王总` → 剥完只剩 `王`（长度 1 < 2）→ **不判**，交 LLM。

    单字名在中文里几乎无法确认是否同一人。硬判会制造误并：
    `王` 可能是王总、王经理、老王，完全不同的三个人。
    """
    assert _ruling("王总", "王") == RULING_UNKNOWN


def test_honorific_requires_actual_strip() -> None:
    """两边都没剥掉东西时，相等不算这条规则的功劳（不能污染审计原因）。

    实际上 `a == b` 在上面就早退了，这里测的是「不同名且都没剥掉」→ UNKNOWN。
    """
    result = match_entities("豆包", "糖包", settings=_s())
    assert result.ruling == RULING_UNKNOWN
    assert result.rule != "honorific"


def test_honorific_can_be_disabled_with_empty() -> None:
    """`ENTITY_HONORIFICS=` 显式空 → 禁用称呼剥离（不是用默认值）。

    注意结果是 `DIFFERENT` 而不是 `UNKNOWN`：称呼表空掉之后，
    `张三哥` 就退化成「张三 + 一个字」，于是被**规则 1（单字前缀冲突）**接管，
    判为不同实体。这是正确的降级顺序，不是 bug —— 关掉一条规则后
    后面的规则仍然会按自己的条件判定。
    """
    result = match_entities("张三哥", "张三", settings=_s(ENTITY_HONORIFICS=""))
    assert result.ruling == RULING_DIFFERENT
    assert result.rule == "single_char_prefix", "应该由规则 1 接管"


def test_honorific_disabled_leaves_unrelated_pairs_alone() -> None:
    """关掉称呼表不影响它本来就不管的那些对。"""
    assert _ruling("豆包", "豆包AI", ENTITY_HONORIFICS="") == RULING_SAME


# ============================================================ 规则 3：后缀白名单


@pytest.mark.parametrize(
    "short,long",
    [
        ("豆包", "豆包AI"),
        ("豆包", "豆包助手"),
        ("北京", "北京市"),
        ("腾讯", "腾讯公司"),
    ],
)
def test_suffix_expansion_is_same(short: str, long: str) -> None:
    assert _ruling(short, long) == RULING_SAME
    assert _ruling(long, short) == RULING_SAME, "必须双向"


def test_abbreviation_needs_synonym_table() -> None:
    """**能力边界**：`阿里/阿里巴巴` 是「全称/简称」（多出「巴巴」不是后缀），
    规则 3 判不了 —— 这类只能进同义词表。

    诚实记录边界，比假装规则能覆盖一切更有价值。
    """
    assert _ruling("阿里", "阿里巴巴") == RULING_UNKNOWN
    assert (
        _ruling("阿里", "阿里巴巴", ENTITY_SYNONYMS="阿里=阿里巴巴") == RULING_SAME
    )


def test_suffix_not_in_whitelist_is_unknown() -> None:
    """**能力边界测试**：`项目A/项目A组` 不该合并。

    「组」故意不在默认白名单里。实测这对余弦 0.8801（比很多应合并的还高），
    却是不同实体 —— 这正是向量方案失败的典型样本。
    宁可漏并（同实体多一个节点），不可误并（两实体混成一个）。
    """
    assert "组" not in _s().ENTITY_SUFFIXES.split(",")
    assert _ruling("项目A", "项目A组") == RULING_UNKNOWN


def test_non_prefix_is_unknown() -> None:
    """不是前缀关系就判不了：`项目A`/`项目B` 只差一个字母也不该合并。"""
    assert _ruling("项目A", "项目B") == RULING_UNKNOWN


def test_suffix_can_be_disabled_with_empty() -> None:
    assert _ruling("豆包", "豆包AI", ENTITY_SUFFIXES="") == RULING_UNKNOWN


def test_suffix_whitelist_is_configurable() -> None:
    """改配置 → 行为跟着变（沿用 spec 024/025 的验收标准）。"""
    assert _ruling("项目A", "项目A组") == RULING_UNKNOWN
    assert (
        _ruling("项目A", "项目A组", ENTITY_SUFFIXES="组") == RULING_SAME
    ), "配置了「组」就该命中"


# ============================================================ 规则 4：同义词表


def test_synonym_hit_is_same() -> None:
    """`周会/每周例会` 字面不包含、余弦 0.4806，只能靠词典。"""
    assert _ruling("周会", "每周例会") == RULING_SAME


def test_synonym_is_bidirectional() -> None:
    """`a=b` 声明后，`b/a` 也要命中 —— 词典是等价关系，不是有向映射。"""
    assert _ruling("周会", "每周例会") == RULING_SAME
    assert _ruling("每周例会", "周会") == RULING_SAME


def test_synonym_can_be_configured() -> None:
    assert _ruling("豆包", "糖包") == RULING_UNKNOWN
    assert (
        _ruling("豆包", "糖包", ENTITY_SYNONYMS="豆包=糖包") == RULING_SAME
    )


def test_synonym_empty_disables() -> None:
    assert _ruling("周会", "每周例会", ENTITY_SYNONYMS="") == RULING_UNKNOWN


def test_malformed_synonym_entries_are_ignored() -> None:
    """配置写错不能炸 —— 用户改 `.env` 写漏一个 `=` 很常见。"""
    got = _ruling("周会", "每周例会", ENTITY_SYNONYMS=",,abc,=,x=")
    assert got == RULING_UNKNOWN


# ============================================================ 总开关


def test_rule_disable_keeps_only_prefix_rule() -> None:
    """`ENTITY_RULE_ENABLED=false` → 只有规则 1 生效，其余全跳过。

    这是「完全回退到改动前行为」的定义：改动前只有单字前缀 + LLM。
    """
    s = _s(ENTITY_RULE_ENABLED=False)
    assert match_entities("豆包", "豆包AI", settings=s).ruling == RULING_UNKNOWN
    assert match_entities("周会", "每周例会", settings=s).ruling == RULING_UNKNOWN
    assert match_entities("张三哥", "张三", settings=s).ruling == RULING_UNKNOWN
    # 但规则 1 还在
    assert match_entities("张三", "张三丰", settings=s).ruling == RULING_DIFFERENT


# ============================================================ 边界与健壮性


@pytest.mark.parametrize(
    "a,b", [("", "豆包"), ("豆包", ""), ("", ""), ("豆包", "豆包")]
)
def test_degenerate_inputs_are_unknown(a: str, b: str) -> None:
    """空名/完全相等 → UNKNOWN。相等不该算「规则判同一」，那是没信息量。"""
    assert match_entities(a, b, settings=_s()).ruling == RULING_UNKNOWN


def test_result_carries_reason_for_audit() -> None:
    """每条判定都要能说清依据 —— 这是规则层相对 LLM 的核心优势。"""
    result = match_entities("豆包", "豆包AI", settings=_s())
    assert result.rule == "suffix"
    assert result.reason, "没有 reason 就无法审计"
    assert "AI" in result.reason


# ============================================================ best_candidate


def test_best_candidate_picks_matching_one() -> None:
    hit = best_candidate("豆包", ["项目A", "豆包AI", "张三"], settings=_s())
    assert hit is not None
    assert hit[0] == "豆包AI"


def test_best_candidate_returns_none_when_no_match() -> None:
    assert best_candidate("微信", ["豆包", "项目A"], settings=_s()) is None


def test_best_candidate_is_reproducible() -> None:
    """多个候选都命中时取排序后的第一个 —— 不能依赖 list 顺序。

    否则同一份数据在不同时刻可能解析出不同的 canonical，图会抖动。
    """
    first = best_candidate("豆包", ["豆包AI", "豆包助手"], settings=_s())
    second = best_candidate("豆包", ["豆包助手", "豆包AI"], settings=_s())
    assert first is not None and second is not None
    assert first[0] == second[0]


def test_best_candidate_skips_self() -> None:
    """别名自己不能当自己的 canonical。"""
    assert best_candidate("豆包", ["豆包"], settings=_s()) is None


# ============================================================ 旧 API 兼容


def test_single_char_prefix_still_importable_from_old_location() -> None:
    """`alias_resolver` 里的旧导入路径必须仍然可用（有测试在 patch 它）。"""
    from memory.services.alias_resolver import (
        PREFIX_CONFLICT_REASON as OLD_REASON,
        single_char_name_prefix_conflict as old_fn,
    )

    assert old_fn("张三", "张三丰") is True
    assert old_fn("豆包", "豆包AI") is False
    assert OLD_REASON == "单字名前缀冲突"

# ============================================================ spec 031：地名后缀


@pytest.mark.parametrize(
    "short,long",
    [
        ("上海", "上海市"),
        ("成都", "成都市"),
        ("苏州", "苏州市"),
        ("杭州", "杭州市"),
        ("广州", "广州市"),
        ("南京", "南京市"),
        ("郑州", "郑州市"),
    ],
)
def test_place_name_with_suffix_is_same(short, long) -> None:
    """地名 + 后缀（市/省/区）必须是同一实体。

    **这是 spec 031 修的真 bug**：`上海` 的结构是「姓 + 单字名」（`上` 在
    姓氏表里），规则 1 抢先判了 DIFFERENT，规则 3（后缀白名单）永远轮不到。

    `北京/北京市` 当年侥幸对了，是因为 `北` 不在姓氏表 —— 026 的 14 组对照
    里没有一组是「姓氏字开头的地名」，这是它的盲区。
    """
    s = Settings()
    r = match_entities(short, long, settings=s)
    assert r.is_same, f"{short}/{long} 被判成 {r.ruling}（{r.reason}），应该是同一地"
    assert r.rule == "suffix", f"应该走后缀规则，实际走了 {r.rule}"


def test_surname_like_place_does_not_break_real_names() -> None:
    """修地名不能把「张三 / 张三丰」也放过去。

    「丰」不在 `ENTITY_SUFFIXES` 里，所以规则 1 仍然判真冲突。
    这是规则 1 存在的**全部理由**，不能被这次修改废掉。
    """
    s = Settings()
    for short, long in [("张三", "张三丰"), ("李四", "李四光")]:
        r = match_entities(short, long, settings=s)
        assert r.ruling == RULING_DIFFERENT, (
            f"{short}/{long} 应该判 DIFFERENT，实际 {r.ruling}（{r.reason}）"
        )
        assert r.rule == "single_char_prefix"


def test_group_suffix_stays_unknown_not_same() -> None:
    """`项目A / 项目A组` 必须保持 UNKNOWN，**不能被地名修复顺手合并**。

    026 特意把「组」排除在 `ENTITY_SUFFIXES` 之外，因为「项目A组」是
    另一个组织，不是「项目A」的别名。

    **注意期望是 UNKNOWN 不是 DIFFERENT**：`split_name_surname("项目A")`
    返回 None（`项目` 不是姓名结构），规则 1 本来就命中不了，
    结果交给 LLM。改前改后都是 UNKNOWN —— 这条测的是「没被改坏」。
    """
    s = Settings()
    r = match_entities("项目A", "项目A组", settings=s)
    assert not r.is_same, "「组」被当成后缀了 —— 026 的决策被破坏"
    assert r.ruling == RULING_UNKNOWN, (
        f"期望 UNKNOWN（交给 LLM），实际 {r.ruling}（{r.reason}）"
    )


def test_place_name_degrades_to_unknown_when_rules_off() -> None:
    """规则层关闭时，地名退化为 UNKNOWN，**不能是 DIFFERENT**。

    这是「弃权」设计的关键验证：规则 1 遇到已知后缀时不下结论，
    后缀规则又因为 `ENTITY_RULE_ENABLED=false` 不执行 ——
    于是整条链的答案是「不知道」，交给 LLM。

    如果规则 1 当初写的是「判 SAME」而不是「弃权」，关闭规则层后
    地名会被错误地合并；如果写「照旧判 DIFFERENT」，就是原来那个 bug。
    """
    s = Settings()
    s.ENTITY_RULE_ENABLED = False
    r = match_entities("上海", "上海市", settings=s)
    assert r.ruling == RULING_UNKNOWN, (
        f"规则层关闭时期望 UNKNOWN，实际 {r.ruling}（{r.reason}）"
    )
    assert not r.is_same


def test_honorific_exception_still_works() -> None:
    """称呼例外没被后缀例外破坏（回归）。"""
    s = Settings()
    r = match_entities("张三", "张三哥", settings=s)
    assert r.is_same
    assert r.rule == "honorific"
