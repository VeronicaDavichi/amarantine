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


def _prefix_conflict_excluding_honorific(
    short: str, long: str, honorifics: list[str]
) -> bool:
    """单字前缀冲突，但**多出来的那个字若是称呼，就不算冲突**。

    为什么需要这个例外：「张三哥 → 张三」里多出的「哥」是称呼，
    不是名字的一部分，应该走「称呼剥离」判为同一人。
    而「张三丰」里的「丰」是名字的一部分，才是真冲突。

    不排除的话，规则 1 会先把 `张三哥/张三` 判成不同实体，
    规则 2（称呼剥离）永远没机会执行 —— 这是实测时发现的顺序 bug。
    """
    if len(long) == len(short) + 1 and long.startswith(short):
        extra = long[-1]
        if extra in honorifics:
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

    **规则顺序不能换**（见各规则的 docstring）：
    1. 单字名前缀冲突 -> DIFFERENT   （必须在 3 之前）
    2. 称呼后缀剥离后相等 -> SAME
    3. 字面包含 + 后缀白名单 -> SAME
    4. 同义词表 -> SAME
    5. 都不命中 -> UNKNOWN（交给 LLM）

    `ENTITY_RULE_ENABLED=false` 时**只有规则 1 保留** —— 因为它是 spec 020
    的独立功能，不属于这次新增；其余规则全部跳过，行为等价于改动前。
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b or a == b:
        return MatchResult(RULING_UNKNOWN, "", "空名或完全相等")

    honorifics = _split_csv(getattr(settings, "ENTITY_HONORIFICS", ""))

    # ---- 规则 1：单字名前缀冲突（双向）----
    # 永远生效，不受 ENTITY_RULE_ENABLED 控制（spec 020 的既有功能）。
    #
    # 但要排除「多出的字是称呼」的情形，否则 `张三哥/张三` 在这里就被判不同，
    # 后面的称呼剥离永远没机会执行（实测发现）。
    if _prefix_conflict_excluding_honorific(
        a, b, honorifics
    ) or _prefix_conflict_excluding_honorific(b, a, honorifics):
        return MatchResult(
            RULING_DIFFERENT, "single_char_prefix", f"{a} / {b} {PREFIX_CONFLICT_REASON}"
        )

    if not getattr(settings, "ENTITY_RULE_ENABLED", True):
        return MatchResult(RULING_UNKNOWN, "", "规则层已关闭")

    # ---- 规则 2：称呼后缀剥离 ----
    if honorifics:
        sa, ra = _strip_honorifics(a, honorifics)
        sb, rb = _strip_honorifics(b, honorifics)
        # 要求「至少一边真的剥掉了」：`张三` vs `张三` 相等不是这条规则的功劳，
        # 记成这条会污染审计日志的原因字段（而且上面 a == b 已经早退了）。
        if (ra or rb) and sa == sb:
            return MatchResult(
                RULING_SAME, "honorific", f"{a} / {b} 称呼剥离后都是「{sa}」"
            )

    # ---- 规则 3：字面包含 + 后缀白名单 ----
    suffixes = _split_csv(getattr(settings, "ENTITY_SUFFIXES", ""))
    if suffixes:
        for hit in (
            _suffix_expansion(a, b, suffixes),
            _suffix_expansion(b, a, suffixes),
        ):
            if hit:
                return MatchResult(
                    RULING_SAME, "suffix", f"{a} / {b} 差一个后缀「{hit}」"
                )

    # ---- 规则 4：同义词表 ----
    synonyms = _parse_synonyms(getattr(settings, "ENTITY_SYNONYMS", ""))
    if synonyms and frozenset((a, b)) in synonyms:
        return MatchResult(RULING_SAME, "synonym", f"{a} / {b} 在同义词表里")

    # ---- 规则 5：判不了 ----
    return MatchResult(RULING_UNKNOWN, "", "规则层无命中")


def best_candidate(
    alias: str, candidates: list[str], *, settings: Settings
) -> tuple[str, MatchResult] | None:
    """在候选里找第一个被判为同一实体的。没有则返回 None。

    返回 `(候选名, 判定结果)`。多个候选都判 SAME 时取**排序后第一个**，
    保证结果可复现（不依赖 dict/list 的偶然顺序）。
    """
    for cand in sorted(set(candidates)):
        if not cand or cand == alias:
            continue
        result = match_entities(alias, cand, settings=settings)
        if result.is_same:
            return cand, result
    return None
