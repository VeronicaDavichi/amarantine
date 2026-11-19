#!/usr/bin/env python3
"""召回质量评测（query → 该找到哪几张卡）。

## 为什么用原文片段当 query，不用摘要

拿卡片摘要当检索词是**循环论证**：摘要本来就是 LLM 从这批原文里生成的，
它的词向量天然接近自己的卡 —— 测出来的 hit@1 会虚高。
（这与 AGENTS.md 里「禁止用摘要措辞反查原文」是同一条规矩。）

改用**原文片段**：从 `raw_quote` 里取一条真实消息，当成「我记得有句话…」来查，
期望命中的是**由这条原文建出来的那张卡**。这更接近用户真实用法。

## 指标

- `hit@1 / hit@5 / hit@10`：目标卡是否出现在前 k 条
- `MRR`：目标卡名次倒数的平均
- 记录 `elapsed_ms` 的 P50/P95（顺带看延迟）

用法：

    python3 harness/eval_recall.py --namespace qq-10000000001 --sample 60
    python3 harness/eval_recall.py --namespace qq-10000000001 --sample 60 --json out.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import sqlite3
import sys
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

DB = ROOT / "back_end" / "data" / "memory.db"

#: 查询里不该出现的东西：URL / 邮箱 / 纯符号
_URL_RE = re.compile(r"https?://\S+|\S+@\S+\.\S+")
_SYM_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]+")


#: 「昵称：内容」里的昵称前缀（QQ 导出的 turn 格式）
_SPEAKER_RE = re.compile(r"^[^：:\n]{1,24}[：:]\s*")
#: 十六进制/文件哈希/XML 头这类东西不是人话
_JUNK_RE = re.compile(r"[0-9a-fA-F]{16,}|<\?xml|\{\"|\}\s*$")


def clean_query(text: str, *, max_len: int = 60) -> str:
    """把原文搓成一条像人问话的 query。"""
    s = _SPEAKER_RE.sub("", (text or "").strip())
    s = _URL_RE.sub(" ", s)
    s = _SYM_RE.sub(" ", s)
    s = re.sub(r"\[[^\]]{0,20}\]", " ", s)          # [图片] [表情]
    s = re.sub(r"@\S+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len]
    return s


def usable_query(text: str, q: str) -> tuple[bool, str]:
    """这条原文能不能当 query。返回 `(ok, 原因)`。

    太短 / 带 @ 或链接 / 是文件哈希 / 纯符号的消息不适合当检索词 ——
    它们本来就没什么语义，测出来的低分反映的是语料，不是召回系统。
    """
    raw = (text or "").strip()
    if len(q) < 12:
        return False, "太短"
    if len(q) < 0.4 * len(raw) and len(raw) > 40:
        return False, "清洗后丢太多"
    if "@" in raw:
        return False, "含 @"
    if _URL_RE.search(raw):
        return False, "含链接"
    if _JUNK_RE.search(raw):
        return False, "哈希/XML"
    if re.fullmatch(r"[\W_]+", q):
        return False, "纯符号"
    return True, ""


def load_pairs(db: pathlib.Path, namespace: str, sample: int,
               seed: int) -> list[dict]:
    """取 (原文片段, 目标 event_id) 对。

    目标卡的来源是 `MemoryEvent.raw_quote_ids`：卡上记着它由哪些原文建成。
    所以「知道原句 → 应该能找回这张卡」是**有依据的期望**，不是拍脑袋。
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    quotes = con.execute(
        "SELECT quote_id, text, session_id, ts FROM raw_quote "
        "WHERE namespace=? AND length(text) >= 12", (namespace,)).fetchall()
    con.close()
    if not quotes:
        return []

    # 原文 → 卡片（走 Weaviate）
    import httpx

    def cards_of_quote(qid: str) -> list[str]:
        q = ('{ Get { MemoryEvent(where: {operator: And, operands: ['
             '{path:["namespace"], operator: Equal, valueText:"%s"},'
             '{path:["raw_quote_ids"], operator: Equal, valueText:"%s"}]}, limit: 5)'
             ' { event_id } } }' % (namespace, qid))
        try:
            r = httpx.post("http://127.0.0.1:8081/v1/graphql",
                           json={"query": q}, timeout=30).json()
            rows = (((r.get("data") or {}).get("Get") or {}).get("MemoryEvent") or [])
            return [x["event_id"] for x in rows if x.get("event_id")]
        except Exception:  # noqa: BLE001 - 单条失败跳过
            return []

    random.seed(seed)
    random.shuffle(quotes)
    pairs: list[dict] = []
    skipped: dict[str, int] = {}
    for q in quotes:
        if len(pairs) >= sample:
            break
        query = clean_query(q["text"])
        ok, why = usable_query(q["text"], query)
        if not ok:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        cards = cards_of_quote(q["quote_id"])
        if not cards:
            continue
        pairs.append({
            "quote_id": q["quote_id"],
            "query": query,
            "target": cards[0],
            "targets": cards,
            "ts": q["ts"],
            "session": q["session_id"],
        })
    if skipped:
        print(f"跳过不适合当 query 的原文：{skipped}")
    return pairs


def roster_names(namespace: str) -> set[str]:
    """名册里的名字（用来把样本分成「含人名」/「纯闲聊」两档）。

    **从本地身份文件读**，不查 Weaviate：Alias 在 Neo4j，不在图数据库层，
    之前写成查 Weaviate 的 Alias 结果永远是空集，分组诊断全归到「纯闲聊」。
    """
    out: set[str] = set()
    for f in (ROOT / "back_end" / "data" / "corpus" / "qq-identities").glob("group-*.json"):
        try:
            members = json.loads(f.read_text(encoding="utf-8")).get("members") or []
        except Exception:  # noqa: BLE001 - 单个文件坏了跳过
            continue
        for m in members:
            for key in ("card", "nickname"):
                name = str(m.get(key) or "").strip()
                if 2 <= len(name) <= 12:
                    out.add(name)
    return out


def run(pairs: list[dict], namespace: str, top_k: int) -> dict:
    from memory.config import load_settings
    from memory.services.recall import recall

    settings = load_settings()
    names = roster_names(namespace)
    hits_at = {1: 0, 5: 0, 10: 0}
    rr_sum = 0.0
    lat: list[float] = []
    rows: list[dict] = []

    for i, p in enumerate(pairs, 1):
        try:
            res = recall(p["query"], namespace=namespace, settings=settings)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响整体
            rows.append({**p, "rank": None, "error": str(exc)})
            print(f"  [{i}/{len(pairs)}] 失败: {exc}")
            continue
        ids = [m.get("event_id") for m in (res.materials or [])][:top_k]
        rank = None
        for r, eid in enumerate(ids, 1):
            if eid in p["targets"]:
                rank = r
                break
        if rank:
            for k in hits_at:
                if rank <= k:
                    hits_at[k] += 1
            rr_sum += 1.0 / rank
        lat.append(res.elapsed_ms or 0.0)
        named = any(n in p["query"] for n in names) if names else False
        rows.append({**p, "rank": rank, "n_returned": len(ids),
                     "has_name": named, "elapsed_ms": res.elapsed_ms})
        mark = f"@{rank}" if rank else "✗"
        print(f"  [{i}/{len(pairs)}] {mark:<4} {p['query'][:34]}")

    n = len([r for r in rows if r.get("rank") is not None or not r.get("error")])
    n = max(1, n)
    def _rate(sub: list[dict], k: int) -> float:
        return (sum(1 for r in sub if r.get("rank") and r["rank"] <= k)
                / len(sub)) if sub else 0.0

    named = [r for r in rows if r.get("has_name")]
    plain = [r for r in rows if not r.get("has_name")]
    summary = {
        "n": len(pairs),
        "含人名样本": len(named),
        "含人名 hit@5": _rate(named, 5),
        "纯闲聊样本": len(plain),
        "纯闲聊 hit@5": _rate(plain, 5),
        "hit@1": hits_at[1] / n,
        "hit@5": hits_at[5] / n,
        "hit@10": hits_at[10] / n,
        "mrr": rr_sum / n,
        "p50_ms": statistics.median(lat) if lat else 0.0,
        "p95_ms": (statistics.quantiles(lat, n=20)[18] if len(lat) >= 20
                   else (max(lat) if lat else 0.0)),
    }
    return {"summary": summary, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="召回质量评测（原文片段 → 目标卡）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--json", default="", help="把明细写到文件")
    args = ap.parse_args()

    pairs = load_pairs(pathlib.Path(args.db), args.namespace, args.sample, args.seed)
    if not pairs:
        print("没有可用的 (原文, 目标卡) 对 —— 检查 raw_quote 与 raw_quote_ids 是否对得上")
        return 1
    print(f"样本 {len(pairs)} 条（原文片段 → 目标卡）\n")

    out = run(pairs, args.namespace, args.top_k)
    s = out["summary"]
    print("\n=== 召回质量 ===")
    print(f"  样本 {s['n']}")
    print(f"  hit@1  {s['hit@1']:.3f}")
    print(f"  hit@5  {s['hit@5']:.3f}")
    print(f"  hit@10 {s['hit@10']:.3f}")
    print(f"  MRR    {s['mrr']:.3f}")
    print(f"  延迟   P50 {s['p50_ms']:.0f}ms / P95 {s['p95_ms']:.0f}ms")

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n明细 → {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
