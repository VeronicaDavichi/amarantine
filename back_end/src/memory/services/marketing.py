"""营销内容识别与召回闸门（spec 040 扩展）。

## 为什么要它

个人 QQ 里有大量「补款/上新/团购」群：一天几十条商品链接 + 图片哈希，
单条最长 4137 字符，30 条拼一片能到 3 万字符 —— LLM 切不动、embedding 413，
而且**对记忆系统毫无价值**。

## 闸门逻辑（用户定的规矩）

判定为营销后，**当场拿关键术语去查已有记忆**：

- 召回得到相关材料（例如以前记过「尾款」「某个品牌的预定」）→ 保留
- 召不回来 → 纯垃圾，不存

判据是「与已有记忆的相关性」，不是「内容像不像广告」——
用户在关注的品牌上新，本来就该留。
"""

from __future__ import annotations

import re

#: URL（淘宝/微信/短链都算）
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
#: 图片/文件哈希占位：`[图片] 926908EA82E9BCD336DAC1EE4DCB8706.png`
_IMAGE_RE = re.compile(r"\[(?:图片|文件|视频|表情)\][^\n]{0,80}")
#: 32 位十六进制串（图片/文件哈希）
_HASH_RE = re.compile(r"\b[0-9A-Fa-f]{16,64}\b")

#: 营销语汇
MARKETING_WORDS = (
    "尾款", "补款", "上新", "预定", "预购", "定金", "拍下", "链接", "团购",
    "现货", "到货", "代购", "价格", "包邮", "下单", "开团", "拼团", "特价",
    "限时", "优惠", "折扣", "库存", "补尾款",
)

#: 判定阈值
URL_RATIO_THRESHOLD = 0.12      # URL 字符占比 ≥12%
IMAGE_LINES_THRESHOLD = 4       # 图片/文件占位行 ≥4
KEYWORD_HITS_THRESHOLD = 2      # 营销词 ≥2 且行数 ≥3


def looks_like_marketing(text: str) -> tuple[bool, dict]:
    """判定一条消息是不是营销内容。

    Returns:
        `(is_marketing, detail)`；`detail` 带判据，便于报告与调参。

    三条判据满足任一即算营销：

    1. URL 字符占比 ≥ 12%（商品链接轰炸）
    2. 图片/文件占位 ≥ 4 行（晒图/补款图）
    3. 营销词 ≥ 2 且总行数 ≥ 3
    """
    body = text or ""
    if not body.strip():
        return False, {"reason": "empty"}

    url_chars = sum(len(m.group(0)) for m in _URL_RE.finditer(body))
    url_ratio = url_chars / max(1, len(body))
    image_lines = len(_IMAGE_RE.findall(body))
    words = [w for w in MARKETING_WORDS if w in body]
    lines = [ln for ln in body.splitlines() if ln.strip()]

    detail = {
        "url_ratio": round(url_ratio, 3),
        "url_chars": url_chars,
        "image_lines": image_lines,
        "marketing_words": words[:5],
        "lines": len(lines),
    }
    hit = (
        url_ratio >= URL_RATIO_THRESHOLD
        or image_lines >= IMAGE_LINES_THRESHOLD
        or (len(words) >= KEYWORD_HITS_THRESHOLD and len(lines) >= 3)
    )
    return hit, detail


def recall_terms(text: str, *, limit: int = 5) -> list[str]:
    """从营销内容里抽「值得去查记忆」的术语。

    去掉 URL、图片占位、哈希、价格与量词，只留**中文/英文词**：

    ```
    【本日开启尾款】鬼灭之刃 无限城篇 cafe3期
      → ["鬼灭之刃", "无限城篇", "本日开启尾款"]
    ```

    这些术语才是「用户关心什么」的信号 —— 广告模板词（补款/上新）会被过滤。
    """
    body = _IMAGE_RE.sub(" ", text or "")
    body = _URL_RE.sub(" ", body)
    body = _HASH_RE.sub(" ", body)

    # 按非词字符切，保留中英文数字串
    tokens = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,12}", body)
    out: list[str] = []
    for tok in tokens:
        if tok.isdigit():
            continue
        if tok in MARKETING_WORDS:
            continue
        if tok in out:
            continue
        out.append(tok)
        if len(out) >= limit:
            break
    return out
