"""历史聊天导入的纯逻辑层（spec 040）。

CLI 在 `harness/import_chat.py`；这里只放**可以单测**的部分：

| 函数 | 职责 |
|---|---|
| `normalize_message` | contentType → 文本（含链接分享的 `source_url`） |
| `privacy_hits` | 敏感信息闸门（证件号 / 手机 / 卡号 / 口令 / 2FA） |
| `chunk_turns` | 按条数切窗 |
| `dedupe_key_for` | 幂等键（窗口 + 序号） |
| `assign_event_dates` | 把切出来的事件映射回源消息时间 |

为什么把「切分后的事件 → 消息时间」也放这里：切分是服务端做的，
但**时间继承的判据**属于导入契约（A6/A7），必须能单测。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Iterator, Sequence

#: 无正文的类型（图片 / 日程 / 通话 / 附件占位）
SKIP_CONTENT_TYPES = {2, 2900, 1101, 501, 1400, 1500}
#: 含正文的类型：纯文本 / 引用回复 / 富文本 / 链接分享
KEEP_CONTENT_TYPES = {1, 1200, 3100, 102}

#: 2FA 六位码：整句只有 6 位数字，或前面带「验证码/动态码/otp」等词
_SIX_DIGITS = re.compile(r"^\d{6}$")
_2FA_CONTEXT = re.compile(r"(?:验证码|动态码|一次性密码|otp)\s*[:：]?\s*\d{6}", re.I)
#: 敏感信息规则（命中即不入库，进报告）
PRIVACY_RULES: dict[str, re.Pattern[str]] = {
    "身份证号": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "手机号": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "银行卡号": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "口令": re.compile(r"(?:密码|口令|password)\s*[:：=]", re.I),
}


def _json(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    try:
        data = json.loads(content or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _extension(content: dict[str, Any]) -> dict[str, Any]:
    att = (content.get("attachments") or [{}])[0]
    try:
        ext = json.loads(att.get("extension") or "{}")
        return ext if isinstance(ext, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def normalize_message(raw: dict[str, Any]) -> tuple[str | None, str]:
    """把一条源消息归一成文本。

    Returns:
        `(text, reason)`。`text is None` 时 `reason` 说明为什么丢弃。

    链接分享（`contentType=102`）**必须带 URL** —— 只取标题的话，
    地址类信息（GitLab / Jira）会一个字都进不了库（本 spec 的坑 #1）。
    """
    content = _json(raw.get("content")) if "content" in raw else _json(raw)
    ctype = raw.get("contentType") or content.get("contentType")
    if ctype is None:
        ctype = 1

    if ctype in SKIP_CONTENT_TYPES:
        return None, f"type{ctype} 无正文"
    if ctype not in KEEP_CONTENT_TYPES:
        return None, f"type{ctype} 未知类型"

    if ctype == 1:
        text = (content.get("text") or "").strip()
        return (text, "") if text else (None, "空正文")

    ext = _extension(content)
    if ctype == 1200:  # 引用回复：取 #### 后的回复正文
        text = (ext.get("title") or "").strip()
    elif ctype == 3100:  # 富文本：取 desc 纯文本
        text = (ext.get("desc") or "").strip()
    else:  # 102 链接分享：标题 + 真实地址
        title = (ext.get("title") or ext.get("text") or "").strip()
        url = (ext.get("source_url") or ext.get("url") or "").strip()
        text = f"{title} {url}".strip()
    return (text, "") if text else (None, f"type{ctype} 空正文")


def privacy_hits(text: str) -> list[str]:
    """返回命中的敏感类别（空列表 = 可以入库）。"""
    hits: list[str] = []
    stripped = (text or "").replace(" ", "")
    if _SIX_DIGITS.match(stripped) or _2FA_CONTEXT.search(text or ""):
        hits.append("2FA")
    for label, pattern in PRIVACY_RULES.items():
        if pattern.search(text or ""):
            hits.append(label)
    # 18 位身份证同时满足 16-19 位卡号规则 —— 前者更具体，只报身份证
    if "身份证号" in hits and "银行卡号" in hits:
        hits.remove("银行卡号")
    return hits


def chunk_turns(turns: Sequence[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """按条数切窗。`size <= 0` 时整批一片。"""
    if not turns:
        return []
    step = size if size and size > 0 else len(turns)
    return [list(turns[i:i + step]) for i in range(0, len(turns), step)]


def dedupe_key_for(cid: str, turns: Sequence[dict[str, Any]], index: int,
                   prefix: str = "dt") -> str:
    """幂等键：`prefix:cid:起:止:序号`。

    必须带**序号** —— 同一天可能切成多片，只有日期区间会撞键。
    """
    stamps = [t["ts"][:10] for t in turns if t.get("ts")]
    start = min(stamps) if stamps else "unknown"
    end = max(stamps) if stamps else "unknown"
    return f"{prefix}:{cid}:{start}:{end}:c{index:02d}"


def _cjk_grams(text: str, n: int = 4) -> set[str]:
    chars = "".join(c for c in (text or "") if "\u4e00" <= c <= "\u9fa5")
    return {chars[i:i + n] for i in range(0, max(0, len(chars) - n + 1))}


def assign_event_dates(
    parts: Iterable[str],
    turns: Sequence[dict[str, Any]],
    *,
    min_overlap: int = 2,
) -> list[str | None]:
    """把切分出来的事件映射回源消息日期。

    判据：事件摘要与某条 turn 的**汉字 4-gram 交集**最大且 ≥ `min_overlap`。
    摘要经过改写，精确子串不可靠；4-gram 交集对改写有一定容忍度。

    Returns:
        与 `parts` 等长的日期列表（`YYYY-MM-DD` 或 `None`）。
        **`None` 表示判不出，绝不回落到今天**（A7）。
    """
    prepared = [(_cjk_grams(t.get("text") or ""), t.get("ts")) for t in turns]
    out: list[str | None] = []
    for part in parts:
        grams = _cjk_grams(part)
        best_ts: str | None = None
        best_score = 0
        for turn_grams, ts in prepared:
            if not ts:
                continue
            score = len(grams & turn_grams)
            if score > best_score:
                best_ts, best_score = ts, score
        out.append(best_ts[:10] if best_ts and best_score >= min_overlap else None)
    return out
