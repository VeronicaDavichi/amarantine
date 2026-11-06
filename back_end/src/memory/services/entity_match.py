"""实体同一性的**确定性规则层**（ADR C.B3 的 ② 级）。

## 为什么这里不是向量

ADR 原方案是「实体名 embedding 余弦 >= 0.92」。实测（bge-m3，14 组中文对照）
证明**这个特征不可分**：

    应合并最低   0.4806  周会 / 每周例会
    不该合并最高 0.8801  项目A / 项目A组

区间完全重叠。按 0.92 执行，8 组应合并里只有 2 组能过线（漏 6 组）；
按 0.85 执行，误并 2 组 + 漏并 5 组。**任何单一阈值都同时误并和漏并。**

更糟的是排序是反的：

    豆包 / 豆包AI   0.7763   应合并
    项目A / 项目A组 0.8801   不该合并   <- 更高

这和 016 记录的「编辑距离被否定」是同一模式的第二次重演。

## 为什么是规则

因为要判定的东西是**语义知识**，不是数值距离：
「豆包AI 是豆包的产品名」这个事实，向量给不了。

规则层也不给，但规则层能**确定性地**覆盖「有明确字面规律」的情形，
把剩下的交给 LLM。这比「所有情形都靠向量猜」更可审计 ——
每条判定都能说清依据，这正是 ADR C.B3 的原话：

> 别名表是可复查的事实，模型推理不是。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from memory.config import Settings
from memory.services.labeler import split_name_surname

logger = logging.getLogger(__name__)

#: 判同一实体
RULING_SAME = "same"
#: 判不同实体
RULING_DIFFERENT = "different"
#: 规则判不了，交给 ③ LLM
RULING_UNKNOWN = "unknown"

#: 「单字名前缀冲突」规则命中的日志前缀，便于测试与排查时 grep
PREFIX_CONFLICT_REASON = "单字名前缀冲突"

#: 称呼剥离后允许的最短长度。低于它就没法判定（`王总 -> 王` 是单字名，
#: 而单字名在中文里几乎无法确认是否同一人）。
_MIN_NAME_AFTER_STRIP = 2


@dataclass(frozen=True)
class MatchResult:
    """一次规则判定的结果。`rule` 和 `reason` 用于审计日志。"""

    ruling: str
    rule: str = ""
    reason: str = ""

    @property
    def is_same(self) -> bool:
        return self.ruling == RULING_SAME


def single_char_name_prefix_conflict(short: str, long: str) -> bool:
    """`short` 是「姓+单字名」，且 `long` 只比它多一个字？

    命中意味着「很可能不是同一人」。这条规则**必须排在字面包含规则之前**，
    否则「张三/张三丰」在「丰」进了后缀白名单时会被误判成同一实体。

    为什么需要它（实测数据）：编辑距离区分不了这两类 ——
    「豆包/豆包AI」（同人）相似度 0.50，「张三/张三丰」（不同人）0.67，
    该合并的反而更低。唯一的可靠差异是**中文人名的结构**。

    只挡「长度差 1」：差 2 以上（`张三` vs `张三丰丰`）有昵称歧义，保守放行。
    """
    if len(long) != len(short) + 1:
        return False
    if not long.startswith(short):
        return False
    return split_name_surname(short) is not None


def _prefix_conflict_excluding_affix(
    short: str, long: str, honorifics: list[str], suffixes: list[str]
) -> bool:
    """单字前缀冲突，但**多出来的那个字若是词缀（称呼/后缀），就不算冲突**。

    「弃权」的语义是「这条规则不下结论」，不是「判同一」——
    结论交给后面的规则。这样 `ENTITY_RULE_ENABLED=false` 时，
    地名会退化成 UNKNOWN（交给 LLM），而不是错的 DIFFERENT。

    两类例外，都是实测撞出来的顺序 bug：

    1. **称呼**：「张三哥 → 张三」里多出的「哥」是称呼，不是名字的一部分，
       该走「称呼剥离」判同一人。不排除的话规则 1 先判 DIFFERENT，
       规则 2 永远没机会执行。
    2. **后缀**：「上海 → 上海市」里多出的「市」在 `ENTITY_SUFFIXES` 里，
       该走「后缀展开」判同一地。不排除的话同样是规则 1 抢先。

    **「张三丰」里的「丰」不是词缀**，所以仍然判真冲突 —— 这正是规则 1
    存在的理由，不能因为加例外把它废掉。

    边界：「项目A / 项目A组」里的「组」**故意不在** `ENTITY_SUFFIXES`
    （spec 026 的决策：两个部门不是同一个），所以走不到这个例外，
    仍然判 DIFFERENT。
    """
    if len(long) == len(short) + 1 and long.startswith(short):
        extra = long[-1]
        if extra in honorifics or extra in suffixes:
            return False
    return single_char_name_prefix_conflict(short, long)


def _split_csv(raw: str) -> list[str]:
    """逗号分隔 → 去空、去重、保序。

    显式空字符串返回 `[]`（= 禁用该规则），这不是「用默认值」。
    惯例和 config.py 里那几个「有意不接」的项一致：给了空就是不要这个功能。
    """
    seen: dict[str, None] = {}
    for part in raw.split(","):
        item = part.strip()
        if item:
            seen.setdefault(item, None)
    return list(seen)


def _strip_honorifics(name: str, honorifics: list[str]) -> tuple[str, int]:
    """反复剥掉结尾的称呼，返回 (剥后, 剥掉几个字)。

    循环剥是为了「王总经理」→「王」这种叠加称呼。
    但剥完不能短于 `_MIN_NAME_AFTER_STRIP`，否则「王总 → 王」会变成
    一个无法判定的单字名，反而制造误判。
    """
    stripped = name
    removed = 0
    changed = True
    while changed:
        changed = False
        for h in honorifics:
            if not h:
                continue
            if stripped.endswith(h) and len(stripped) - len(h) >= _MIN_NAME_AFTER_STRIP:
                stripped = stripped[: -len(h)]
                removed += len(h)
                changed = True
                break
    return stripped, removed


def _suffix_expansion(short: str, long: str, suffixes: list[str]) -> str | None:
    """`short` 是 `long` 的前缀，返回多出的部分（若它在白名单里）。

    **不是「谁包含谁就合并」**：`项目A` / `项目A组` 也是包含关系，
    但「组」故意不在白名单里 —— 实测这种情形余弦 0.8801 却是不同实体。

    返回命中的后缀，未命中返回 None。
    """
    if len(long) <= len(short) or not long.startswith(short):
        return None
    tail = long[len(short) :]
    return tail if tail in suffixes else None


def _parse_synonyms(raw: str) -> set[frozenset[str]]:
    """`a=b,c=d` → 无序对集合（双向生效）。"""
    pairs: set[frozenset[str]] = set()
    for part in raw.split(","):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        left, _, right = chunk.partition("=")
        a, b = left.strip(), right.strip()
        if a and b and a != b:
            pairs.add(frozenset((a, b)))
    return pairs


def match_entities(a: str, b: str, *, settings: Settings) -> MatchResult:
    """判定两个实体名是否同一。三态：SAME / DIFFERENT / UNKNOWN。

    **所有规则都跑一遍再裁决**，不是「首个命中就返回」。

    为什么改（spec 032）：旧实现是顺序决定结论，于是「上海/上海市」在
    规则 1（姓名结构，DIFFERENT）和规则 3（后缀，SAME）之间来回震荡，
    最终答案取决于哪条规则排在前面 —— 031 那个 bug 正是这么来的。
    031 的修法是「让规则 1 弃权」，能治当前这一例，但每加一条规则就要
    再加一个例外，越修越脆。

    现在的结构：

    1. 收集所有规则的结论（SAME 票 / DIFFERENT 票）
    2. **同义词表命中 -> SAME**（用户明确声明，压过所有启发式猜测）
    3. 只有一种结论 -> 用它
    4. 两种结论都有 -> UNKNOWN / rule="conflict"（震荡，交给 LLM）
    5. 没有任何结论 -> UNKNOWN

    规则顺序**不再影响结论**（除了同义词的优先级，那是有意的）。

    `ENTITY_RULE_ENABLED=false` 时只有规则 1 参与 —— 它是 spec 020 的
    独立功能。同义词也属于规则层，一并关闭，行为退化为「全部交 LLM」。
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b or a == b:
        return MatchResult(RULING_UNKNOWN, "", "空名或完全相等")

    honorifics = _split_csv(getattr(settings, "ENTITY_HONORIFICS", ""))
    suffixes = _split_csv(getattr(settings, "ENTITY_SUFFIXES", ""))
    rules_enabled = getattr(settings, "ENTITY_RULE_ENABLED", True)

    # ---- 收集票 ----
    same_hits: list[tuple[str, str]] = []  # [(rule, reason)]
    diff_hits: list[tuple[str, str]] = []

    # 规则 1：单字名前缀冲突（双向）。永远生效，不受 ENTITY_RULE_ENABLED 控制。
    #
    # 「弃权」例外（多出的字是称呼或后缀时不表态）仍然保留 —— 它和投票机制
    # 不冲突：弃权是「这条规则知道自己在这种情形下不可靠」，投票是
    # 「多条规则都表态时才需要裁决」。保留弃权能**减少不必要的 LLM 调用**：
    # 「上海/上海市」明显是同一地，没必要为了裁决去问一次模型。
    if _prefix_conflict_excluding_affix(
        a, b, honorifics, suffixes
    ) or _prefix_conflict_excluding_affix(b, a, honorifics, suffixes):
        diff_hits.append(
            ("single_char_prefix", f"{a} / {b} {PREFIX_CONFLICT_REASON}")
        )

    if rules_enabled:
        # 规则 2：称呼后缀剥离
        if honorifics:
            sa, ra = _strip_honorifics(a, honorifics)
            sb, rb = _strip_honorifics(b, honorifics)
            # 要求「至少一边真的剥掉了」：`张三` vs `张三` 相等不是这条规则的功劳。
            if (ra or rb) and sa == sb:
                same_hits.append(
                    ("honorific", f"{a} / {b} 称呼剥离后都是「{sa}」")
                )

        # 规则 3：字面包含 + 后缀白名单
        if suffixes:
            for hit in (
                _suffix_expansion(a, b, suffixes),
                _suffix_expansion(b, a, suffixes),
            ):
                if hit:
                    same_hits.append(
                        ("suffix", f"{a} / {b} 差一个后缀「{hit}」")
                    )
                    break

        # 规则 4：同义词表
        synonyms = _parse_synonyms(getattr(settings, "ENTITY_SYNONYMS", ""))
        if synonyms and frozenset((a, b)) in synonyms:
            same_hits.append(("synonym", f"{a} / {b} 在同义词表里"))

    # ---- 裁决 1：用户声明优先 ----
    # 同义词是**明确知识**，规则 1/2/3 都是**启发式猜测**。
    # 明确知识被猜测覆盖是设计错误 —— 实测过：配了 `张三丰=张三` 之后，
    # 规则 1 仍然抢先返回 DIFFERENT，同义词表根本没机会执行。
    #
    # 这里也不交给 LLM：LLM 看到「张三 / 张三丰」也大概率说不同，
    # 用户的声明等于白配。用户说是同一个，就是同一个。
    for rule, reason in same_hits:
        if rule == "synonym":
            return MatchResult(RULING_SAME, "synonym", reason)

    # ---- 裁决 2：没有任何规则表态 ----
    if not same_hits and not diff_hits:
        return MatchResult(
            RULING_UNKNOWN,
            "",
            "规则层无命中" if rules_enabled else "规则层已关闭",
        )

    # ---- 裁决 3：只有一种结论 ----
    if same_hits and not diff_hits:
        rule, reason = same_hits[0]
        return MatchResult(RULING_SAME, rule, reason)
    if diff_hits and not same_hits:
        rule, reason = diff_hits[0]
        return MatchResult(RULING_DIFFERENT, rule, reason)

    # ---- 裁决 4：震荡 ----
    # 两种结论都有，且没有同义词可以定音 —— 这是真正该交给 LLM 的情形。
    #
    # 为什么不定个默认值：两条启发式都只是「大概率」，没有哪个更可信的依据。
    # 硬选一个就是「按规则顺序碰运气」，031 那个 bug 正是这么来的。
    #
    # `rule="conflict"` 让审计日志能区分「规则打架」和「规则不认识这俩」。
    return MatchResult(
        RULING_UNKNOWN,
        "conflict",
        f"{a} / {b} 规则矛盾 | "
        f"同: {[r for r, _ in same_hits]} "
        f"不同: {[r for r, _ in diff_hits]}",
    )


def best_candidate(
    alias: str, candidates: list[str], *, settings: Settings
) -> tuple[str, MatchResult] | None:
    """在候选里找第一个被判为同一实体的。没有则返回 None。

    返回 `(候选名, 判定结果)`。多个候选都判 SAME 时取**排序后第一个**，
    保证结果可复现（不依赖 dict/list 的偶然顺序）。

    **震荡候选会被跳过**（spec 032）：规则互相矛盾的候选判 UNKNOWN，
    不满足 `is_same`，于是继续试下一个。如果没有别的候选命中，
    调用方拿到 None -> 走 LLM。

    震荡日志在这里打，因为 `match_entities` 是纯判定函数，
    它不知道自己是「在一次候选扫描中被调用」还是「单独被调用」。
    """
    for cand in sorted(set(candidates)):
        if not cand or cand == alias:
            continue
        result = match_entities(alias, cand, settings=settings)
        if result.rule == "conflict":
            # 震荡率是规则设计质量的指标：日志多说明规则职责重叠，
            # 或者数据分布和假设不符。没有这个数字就不知道「交 LLM」
            # 是偶尔兜底还是天天兜底。
            logger.info(
                "规则震荡，交给 LLM | %s / %s | %s", alias, cand, result.reason
            )
        if result.is_same:
            return cand, result
    return None
