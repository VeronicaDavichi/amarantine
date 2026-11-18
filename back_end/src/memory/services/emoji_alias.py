# -*- coding: utf-8 -*-
"""从含 emoji 的昵称推导候选别名（spec 047 延伸）。

## 两条规则

1. **去 emoji**：`便携式🦆🍗 → 便携式`（实测语料出现 139 次，最有效）
2. **emoji 替词**：`🦆🍗 → 鸭腿`、`便携式🦆🍗 → 便携式鸭腿`
   （用户 2026-11-18 提的思路：群里常按 emoji 的意思叫人）

## 为什么必须过「语料验证」

替词猜出来的东西很容易是**臆造**：`便携式🦆🍗 → 便携式鸭腿` 在 4383 张卡里
一次都没出现过。所以本模块只负责**生成候选**，是否采纳由
`harness/emoji_alias_scan.py` 用真实语料里的出现次数决定（默认 ≥3 次）。

## 为什么拒纯数字候选

`🍚09 → 09`、`4🍊4 → 44` 这类去 emoji 后只剩数字，语料里到处都是（哈希、ID），
出现几百次也不代表是称呼 —— 必须排除，否则会造出一堆假别名。
"""

from __future__ import annotations

import json
import pathlib
import re

#: emoji 与各类符号（含变体选择符、零宽连接符、组合键）
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]"
)

#: 去 emoji 后只剩这些的，一律不要（数字/标点/空白）
_USELESS_RE = re.compile(r"^[\d\s\W_]*$")

#: 虚词/口头语：去 emoji 后正好等于这些的，不是名字
_STOPWORDS = frozenset({
    "不是", "就是", "什么", "这个", "那个", "可以", "没有", "我们", "你们",
    "他们", "来点", "有没", "一起", "现在", "时候", "东西", "事情", "真的",
})

#: 拉丁候选的最短长度。`in`/`or`/`Ka`/`LY`/`ii` 这种在语料里出现几百次，
#: 但那只是普通子串，不是称呼。
LATIN_MIN_LEN = 3

#: `src/memory/services/emoji_alias.py` → 往上 4 层才是仓库根
#: （services → memory → src → back_end → repo）。写成 parents[2] 会指到
#: `back_end/back_end/config`，结果词表永远读不到、emoji 一律只剩「去 emoji」。
DEFAULT_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "config" / "emoji_words.json"
)


def load_emoji_words(path: pathlib.Path | str | None = None) -> dict[str, str]:
    """读 `emoji_words.json`；坏文件返回空表（推导降级为「只去 emoji」）。"""
    p = pathlib.Path(path) if path else DEFAULT_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 词表缺失不该阻断主流程
        return {}
    return {k: v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, str) and v}


def has_emoji(name: str) -> bool:
    return bool(EMOJI_RE.search(name or ""))


def derive_names(
    name: str,
    *,
    words: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """推导候选别名，返回 `[(候选, 方式)]`，方式 ∈ `{'strip', 'words'}`。

    顺序：先「去 emoji」（更保守），再「替词」（更激进）。
    """
    text = (name or "").strip()
    if not text or not has_emoji(text):
        return []
    table = words if words is not None else load_emoji_words()

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(cand: str, how: str) -> None:
        cand = re.sub(r"\s+", "", cand)
        if not cand or cand in seen or len(cand) < 2:
            return
        if EMOJI_RE.search(cand):
            return
        if _USELESS_RE.match(cand):
            return
        if cand in _STOPWORDS or cand.endswith("的"):
            return
        if cand.isascii() and len(cand) < LATIN_MIN_LEN:
            return
        seen.add(cand)
        out.append((cand, how))

    stripped = EMOJI_RE.sub("", text)
    if stripped:
        add(stripped, "strip")

    if table:
        replaced = "".join(table.get(ch, ch) for ch in text)
        if replaced != text:
            add(replaced, "words")
    return out
