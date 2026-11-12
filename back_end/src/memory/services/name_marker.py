"""人名标记（spec 047）：把「显示名」分类，再决定能不能用。

## 为什么要分类

实测群 `1019369531`（500 人）里同名组 10 个，形态完全不同：

| 名片 | 数量 | 本质 |
|---|---|---|
| `合并转发存在广告,禁言15分钟` | 23 | **管理员处罚标记**，不是名字 |
| `刘芮西宝宝` | 21 | 集体整活（21 个不同的人改同一个梗） |
| `　`（全角空格） | 3 | 整活人（全空白） |
| `往汐（北瑀家的鱼）复活版` | 1 | 真·小号 |
| `秋` / `咸鱼` / `黑崎一护` | 各 2 | 真实同名 |

**不做分类直接按名字关联，会把 21 个人并成一个人。**

## 判定顺序

1. 含处罚关键词 → `moderation`（不是名字）
2. 去掉空白/控制字符后为空 → `blank_ws`（整活人）
3. 去掉标点后为空 → `blank_punct`（无意义名，回落昵称）
4. 带小号后缀 → `alt`（同主人候选）
5. 其余 → `normal`
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

#: 处罚类名片的关键词（群管理把成员名片改成这些当惩戒）
MODERATION_KEYWORDS = (
    "禁言", "广告", "管理禁止", "违规", "违反群规", "警告", "已禁言", "封禁",
)

#: 小号/复活版后缀
ALT_SUFFIXES = (
    "复活版", "小号", "二号机", "三号机", "备用", "转生", "分身", "副本",
    "影分身", "二号", "2号", "3号",
)

#: 同名达到这个数量就判定为整活（实测整活组 13~23 人，真实同名多为 2 人）
IMPERSONATION_MIN = 3

#: 双向控制符：RLO/LRO/PDF/嵌入与隔离符。`AB\u202ECD` 渲染出来像 `ABCD`，
#: 但字节不同 —— 这是**伪装**，不是同名。
BIDI_CONTROLS = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + ["\u200e", "\u200f", "\u061c"]
)

#: 零宽字符：肉眼不可见，同样用于伪装
ZERO_WIDTH = frozenset(
    [chr(0x200B), chr(0x200C), chr(0x200D), chr(0xFEFF), chr(0x2060)]
)

#: 分类结果
KIND_MODERATION = "moderation"
KIND_BLANK_WS = "blank_ws"
KIND_BLANK_PUNCT = "blank_punct"
KIND_ALT = "alt"
KIND_NORMAL = "normal"
#: 伪装名：含双向控制符/零宽字符，渲染相似但字节不同 —— **不是同名**
KIND_SPOOFED = "spoofed"

#: 可以作为「名字」使用的类别
USABLE_KINDS = (KIND_ALT, KIND_NORMAL)


@dataclass(frozen=True)
class CardVerdict:
    """一张名片的判定结果。"""

    kind: str
    base: str = ""
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.kind in USABLE_KINDS


def _is_control(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "C"


def _is_space(ch: str) -> bool:
    return ch.isspace() or unicodedata.category(ch) in ("Zs", "Zl", "Zp")


def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P") or unicodedata.category(ch) == "Sm"


def strip_parens(name: str) -> str:
    """去掉括号内容：`往汐（北瑀家的鱼）` → `往汐`。"""
    return re.sub(r"[（(][^）)]*[）)]", "", name or "").strip()


def strip_alt_suffix(name: str) -> tuple[str, str | None]:
    """去掉小号后缀，返回 `(base, 命中的后缀)`。"""
    text = name or ""
    for suffix in ALT_SUFFIXES:
        if suffix in text:
            return text.replace(suffix, "").strip(), suffix
    return text.strip(), None


def classify_card(name: str) -> CardVerdict:
    """判定一张群名片的类别。

    ```
    「合并转发存在广告,禁言15分钟」 → moderation
    「　」（全角空格）             → blank_ws   （整活人）
    「……」                        → blank_punct（无意义，回落昵称）
    「往汐（北瑀家的鱼）复活版」    → alt（base=往汐）
    「秋」                         → normal
    ```
    """
    text = (name or "").strip()
    if any(kw in text for kw in MODERATION_KEYWORDS):
        return CardVerdict(KIND_MODERATION, reason="处罚标记")

    # 控制字符分两种：
    # - **整张名片只有控制/空白** → 整活人（blank_ws），和空白名同处理
    # - **正常字里夹控制符** → 伪装名（spoofed），base 用原文，永不与干净名同桶
    has_hidden = any(ch in BIDI_CONTROLS or ch in ZERO_WIDTH for ch in text)
    visible_no_ctrl = "".join(
        ch for ch in text
        if not _is_space(ch) and not _is_control(ch)
        and ch not in BIDI_CONTROLS and ch not in ZERO_WIDTH
    )
    if has_hidden and not visible_no_ctrl:
        return CardVerdict(KIND_BLANK_WS, reason="全空白/控制字符")
    if has_hidden:
        return CardVerdict(KIND_SPOOFED, base=text, reason="含双向/零宽控制字符")

    visible = "".join(ch for ch in text if not _is_space(ch) and not _is_control(ch))
    if not visible:
        return CardVerdict(KIND_BLANK_WS, reason="全空白/控制字符")

    # 括号内容先剥掉再判标点 —— 否则「（北瑀家的鱼）」的括号被当标点删掉，
    # 括号里的字会和名字粘在一起（实测 `往汐北瑀家的鱼`）。
    no_paren = strip_parens(visible)
    core = "".join(ch for ch in no_paren if not _is_punct(ch))
    if not core:
        return CardVerdict(KIND_BLANK_PUNCT, reason="全标点")

    base = strip_parens(core)
    base, suffix = strip_alt_suffix(base)
    if suffix:
        return CardVerdict(KIND_ALT, base=base or core, reason=f"小号后缀:{suffix}")
    return CardVerdict(KIND_NORMAL, base=base or core, reason="")


@dataclass
class NameBucket:
    """同一个基础名下的多个账号。"""

    base: str
    accounts: list[dict] = field(default_factory=list)
    kind: str = "normal"          # impersonation / same_owner / ambiguous
    reason: str = ""


def collect_excluded(members: list[dict]) -> list[dict]:
    """收集**不能当名字用**的名片（处罚标记 / 空白 / 伪装名），供审计报告。

    为什么单独收集：这些名字被排除出关联，但**不能静默消失** ——
    运营/排查时需要知道「这个群有 26 个处罚名片、3 个伪装名」。
    """
    out: list[dict] = []
    for m in members:
        # 名片是「全空白」时不能先 strip 成空串再跳过 —— 那是整活人，要进审计。
        raw_card = m.get("card")
        display = raw_card if raw_card not in (None, "") else (m.get("nickname") or "")
        if display == "":
            continue
        verdict = classify_card(display)
        if verdict.usable:
            continue
        out.append({
            "uid": m.get("uid"),
            "qq": m.get("qq"),
            "card": display,
            "kind": verdict.kind,
            "reason": verdict.reason,
        })
    return out


def analyze_group(members: list[dict]) -> list[NameBucket]:
    """把一个群的成员按「基础名」分桶，并给出关联判定。

    Args:
        members: 每项 `{uid, qq, nickname, card}`。

    Returns:
        只返回**有 2 个及以上**账号的桶，`kind` 为：

        - `impersonation`：≥3 个号（整活），**不关联**
        - `same_owner`：2 个号且恰好一个带小号后缀 → 可建 `SAME_OWNER`
        - `ambiguous`：2 个号但都没后缀 → 不关联，只留痕

    Note:
        **伪装名（含双向/零宽控制符）一律不进桶**，连彼此之间也不关联 ——
        这类名字常是**复制粘贴别人的模板**，两个号可能只差一两个字却毫无关系。
        它们由 `collect_excluded` 单独收集，进审计报告而不是静默丢弃。
    """
    buckets: dict[str, NameBucket] = {}
    for m in members:
        raw_card = m.get("card")
        display = raw_card if raw_card not in (None, "") else (m.get("nickname") or "")
        if display == "":
            continue
        verdict = classify_card(display)
        if not verdict.usable:
            continue
        buckets.setdefault(verdict.base, NameBucket(base=verdict.base)).accounts.append(
            {
                "uid": m.get("uid"),
                "qq": m.get("qq"),
                "nickname": m.get("nickname"),
                "card": display,
                "kind": verdict.kind,
            }
        )

    out: list[NameBucket] = []
    for bucket in buckets.values():
        n = len(bucket.accounts)
        if n < 2:
            continue
        alts = [a for a in bucket.accounts if a["kind"] == KIND_ALT]
        if n >= IMPERSONATION_MIN:
            bucket.kind = "impersonation"
            bucket.reason = f"{n} 个号同名，按整活处理（阈值 {IMPERSONATION_MIN}）"
        elif n == 2 and len(alts) == 1:
            bucket.kind = "same_owner"
            bucket.reason = "一个主号 + 一个小号后缀"
        else:
            bucket.kind = "ambiguous"
            bucket.reason = "同名但无小号证据"
        out.append(bucket)
    out.sort(key=lambda b: (-len(b.accounts), b.base))
    return out
