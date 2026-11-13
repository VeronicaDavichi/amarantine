"""拼音首字母缩写解析（spec 048）。

## 为什么需要

中文社群大量用缩写指代全名：

```
lysk = 恋与深空    nn = 暖暖    mz = 母猪    dz = 叠纸/地址
```

记忆里 `lysk` 和 `恋与深空` 是**断开的**：搜全称召回不到缩写卡。

## 候选来源：nbnhhsh

`POST https://lab.magiconch.com/api/nbnhhsh/guess {"text":"lysk"}`

实测它**只给候选、不给答案**（`lysk → 恋与深空 / 老姚烧烤 / 老用手抠`），
所以必须用**本地语料共现**消歧；并且它没收录的词（`nn=暖暖`）要本地词典补。

## 原则

- 有把握才连（`top >= 2 * second` 且 `top >= 2`），否则标 ambiguous
- API 不可达不阻断（读缓存 / 跳过）
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("memory.abbr")

NB_URL = "https://lab.magiconch.com/api/nbnhhsh/guess"

#: 独立 token：前后不是字母（`lysk` 命中，`skyes` 不命中）
_TOKEN_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{2,6})(?![A-Za-z])")

#: 本地补充词典（nbnhhsh 没收的词，如 `nn=暖暖`）
_LOCAL_PATH = Path(__file__).resolve().parents[3] / "config" / "abbr_local.json"

#: 高频非缩写 token（文件后缀/协议/常见英文词），不参与解析
_BLOCKLIST = frozenset({
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "mp4", "mp3", "wav", "pdf", "zip",
    "http", "https", "www", "com", "html", "url", "uid", "token", "api", "json",
    "qq", "id", "ok", "no", "yes", "the", "and", "you", "for", "not", "with",
    "xml", "msg", "src", "img", "emoji", "docx", "xlsx", "txt", "log",
})

#: 需要从语料里剥掉的内容（它们会制造假 token）
_STRIP_PATTERNS = (
    re.compile(r"https?://\S+"),
    re.compile(r"\[\s*(?:图片|文件|视频|动画表情|表情|聊天记录)\s*\][^\n]*"),
    re.compile(r"<\?xml[\s\S]*", re.I),
    re.compile(r"[A-Za-z]:\\[^\s]+"),
    re.compile(r"\b[0-9A-Fa-f]{16,}\b"),
    re.compile(r"\b[0-9A-Fa-f-]{30,}\.(?:jpg|jpeg|png|gif|webp|mp4)\b", re.I),
)

#: 域名词：候选含这些字样时加分（Lo/暖暖圈）
_DOMAIN_WORDS = ("暖暖", "深空", "叠纸", "恋与", "乙女", "周边")


def load_local_dict(path: Path | None = None) -> dict[str, list[str]]:
    """读本地补充词典；不存在返回空表。"""
    p = path or _LOCAL_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k).lower(): [str(x) for x in v] for k, v in data.items()}
    except Exception:  # noqa: BLE001
        return {}


class AbbrCache:
    """nbnhhsh 查询缓存（SQLite）。同一 token 不重复请求。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (_LOCAL_PATH.parent / "abbr_cache.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _ensure(self) -> None:
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS abbr_cache ("
                "term TEXT PRIMARY KEY, candidates TEXT NOT NULL, fetched_at TEXT NOT NULL)"
            )

    def get(self, term: str) -> list[str] | None:
        with self._connect() as con:
            row = con.execute("SELECT candidates FROM abbr_cache WHERE term=?", (term,)).fetchone()
        return json.loads(row["candidates"]) if row else None

    def put(self, term: str, candidates: list[str]) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO abbr_cache(term, candidates, fetched_at) VALUES (?,?,?)",
                (term, json.dumps(candidates, ensure_ascii=False),
                 datetime.now().isoformat(timespec="seconds")),
            )


def guess_abbr(term: str, *, cache: AbbrCache | None = None, timeout: float = 8.0) -> list[str]:
    """查 nbnhhsh 候选；缓存优先，失败返回空表（不抛）。"""
    key = (term or "").strip().lower()
    if not key:
        return []
    cache = cache or AbbrCache()
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            resp = client.post(NB_URL, json={"text": key})
        if resp.status_code >= 400:
            return []
        data = resp.json()
        items = data if isinstance(data, list) else [data]
        out: list[str] = []
        for item in items:
            for t in item.get("trans") or []:
                t = str(t).strip()
                if t and t not in out:
                    out.append(t)
        cache.put(key, out)
        return out
    except Exception as exc:  # noqa: BLE001 - 外部依赖，失败即降级
        logger.warning("nbnhhsh 查询失败 %s: %s", key, exc)
        return []


def clean_text(text: str) -> str:
    """剥掉 URL / 图片占位 / XML / 文件路径 / 哈希，避免制造假 token。"""
    out = text or ""
    for pat in _STRIP_PATTERNS:
        out = pat.sub(" ", out)
    return out


def scan_abbr_tokens(texts: list[str]) -> Counter:
    """统计语料里独立的 2~6 位字母 token。"""
    counter: Counter = Counter()
    for text in texts:
        for m in _TOKEN_RE.finditer(clean_text(text)):
            tok = m.group(1).lower()
            if tok in _BLOCKLIST:
                continue
            counter[tok] += 1
    return counter


def score_candidates(
    abbr: str,
    candidates: list[str],
    texts: list[str],
    *,
    window_chars: int = 200,
) -> list[tuple[str, int]]:
    """用本地语料给候选打分。

    证据：
    - 候选全称在语料里出现：每次 +1
    - 缩写与候选在 ±window 字符内共现：每次 +2
    - 候选含域名词：+1

    Returns:
        `[(candidate, score)]`，按分数降序。
    """
    blob = "\n".join(clean_text(t) for t in texts)
    lower_blob = blob.lower()
    abbr = abbr.lower()
    scored: list[tuple[str, int]] = []
    for cand in candidates:
        if not cand:
            continue
        # 「出现过」只加 1 分 —— 按次数累加会让「什么」这类高频词碾压一切
        # （实测 `sm` 被解析成「什么」，44 次 > 上下文里的 SM）。
        score = 1 if cand in blob else 0
        # 共现：每个缩写出现点看前后窗口
        for m in re.finditer(re.escape(abbr), lower_blob):
            seg = blob[max(0, m.start() - window_chars): m.end() + window_chars]
            if cand in seg:
                score += 2
        if any(w in cand for w in _DOMAIN_WORDS):
            score += 1
        scored.append((cand, score))
    scored.sort(key=lambda x: -x[1])
    return scored


def resolve_abbr(
    abbr: str,
    texts: list[str],
    *,
    cache: AbbrCache | None = None,
    local: dict[str, list[str]] | None = None,
    ratio: float = 2.0,
    min_score: int = 2,
) -> dict[str, Any]:
    """解析一个缩写：候选 → 打分 → 是否可连。

    Returns:
        `{"abbr","candidates":[(name,score)],"picked","ambiguous","source"}`。
        `picked` 为 None 表示无法区分（多义），不建边。
    """
    key = (abbr or "").strip().lower()
    local = local if local is not None else load_local_dict()
    cands = list(guess_abbr(key, cache=cache))
    for extra in local.get(key, []):
        if extra not in cands:
            cands.append(extra)
    if not cands:
        return {"abbr": key, "candidates": [], "picked": None,
                "ambiguous": True, "source": "none"}

    scored = score_candidates(key, cands, texts)
    picked = None
    ambiguous = True
    if scored:
        top_name, top_score = scored[0]
        second = scored[1][1] if len(scored) > 1 else 0
        if top_score >= min_score and top_score >= ratio * max(second, 1):
            picked = top_name
            ambiguous = False
    return {
        "abbr": key,
        "candidates": scored,
        "picked": picked,
        "ambiguous": ambiguous,
        "source": "nbnhhsh+local" if key in local else "nbnhhsh",
    }
