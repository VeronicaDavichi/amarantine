#!/usr/bin/env python3
"""对比旧 namespace vs 重修后 namespace 的召回质量（spec 055 的最终判据）。

## 为什么同一个评测集不能用两次

`load_pairs` 是**从某个 namespace 的 raw_quote 里取原文**当 query，
再找「由这条原文建出来的卡」。新旧两个 namespace 的卡不同，
但原文相同 —— 所以可以用**同一批 query**（来自旧 namespace 的 raw_quote），
分别去两个 namespace 里找。

目标卡的确定方式要分开：
- 旧 namespace：targets = 旧卡里 raw_quote_ids 含该 quote 的
- 新 namespace：targets = 新卡里 raw_quote_ids 含该 quote 的

这样才是公平对比（都是「知道原话，找对应卡」）。

## 用法

    python3 harness/compare_namespaces.py --old qq-10000000001 --new qq-10000000001-v2
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import sqlite3
import statistics
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

DB = ROOT / "back_end" / "data" / "memory.db"

_URL_RE = re.compile(r"https?://\S+|\S+@\S+\.\S+")
_SYM_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]+")
_SPEAKER_RE = re.compile(r"^[^：:\n]{1,24}[：:]\s*")
_JUNK_RE = re.compile(r"[0-9a-fA-F]{16,}|<\?xml|\{\"|\}\s*$")


def clean_query(text: str, max_len: int = 60) -> str:
    s = _SPEAKER_RE.sub("", (text or "").strip())
    s = _URL_RE.sub(" ", s)
    s = _SYM_RE.sub(" ", s)
    s = re.sub(r"\[[^\]]{0,20}\]", " ", s)
    s = re.sub(r"@\S+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def usable(text: str, q: str) -> bool:
    raw = (text or "").strip()
    if len(q) < 12:
        return False
    if len(q) < 0.4 * len(raw) and len(raw) > 40:
        return False
    if "@" in raw or _URL_RE.search(raw) or _JUNK_RE.search(raw):
        return False
    return not re.fullmatch(r"[\W_]+", q)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    from memory.services.local_paths import load_settings_from_project
    from memory.services.recall import recall

    s = load_settings_from_project()
    base = str(s.WEAVIATE_URL).rstrip("/")

    def gql(q: str) -> dict:
        req = urllib.request.Request(
            f"{base}/v1/graphql", data=json.dumps({"query": q}).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=300).read())

    def text2cards(ns: str) -> dict[str, list[str]]:
        """`原文文本 → 卡片列表`。

        **不能按 quote_id 对齐**：`quote_id` 的哈希含 namespace（spec 005），
        同一句话导进两个域得到不同 id。text 才是跨域不变的身份。
        """
        # 先建 quote_id → text
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        q2t = {q: (t or "") for q, t in con.execute(
            "SELECT quote_id, text FROM raw_quote WHERE namespace=?", (ns,))}
        con.close()
        out: dict[str, list[str]] = {}
        offset = 0
        while True:
            d = gql('{ Get { MemoryEvent(where: {path:["namespace"], operator: Equal, '
                    'valueText: "' + ns + '"}, limit: 500, offset: ' + str(offset) + ') '
                    '{ event_id raw_quote_ids } } }')
            rows = ((d.get("data") or {}).get("Get") or {}).get("MemoryEvent") or []
            if not rows:
                break
            for x in rows:
                for q in x.get("raw_quote_ids") or []:
                    t = q2t.get(q)
                    if t:
                        out.setdefault(t, []).append(x["event_id"])
            if len(rows) < 500:
                break
            offset += 500
        return out

    # query 从**旧** namespace 的原文取（原文两边一样）
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    quotes = [(q, (t or ""), ts) for q, t, ts in con.execute(
        "SELECT quote_id, text, ts FROM raw_quote WHERE namespace=? AND length(text)>=12",
        (args.old,))]
    con.close()

    old_map = text2cards(args.old)
    new_map = text2cards(args.new)
    print(f"旧 {args.old}: {sum(len(v) for v in old_map.values())} 关联 / "
          f"{len(old_map)} 条原文有卡")
    print(f"新 {args.new}: {sum(len(v) for v in new_map.values())} 关联 / "
          f"{len(new_map)} 条原文有卡")

    random.seed(args.seed)
    random.shuffle(quotes)
    pairs = []
    skipped: dict[str, int] = {}
    for qid, text, _ts in quotes:
        if len(pairs) >= args.sample:
            break
        q = clean_query(text)
        if not usable(text, q):
            skipped["不合格"] = skipped.get("不合格", 0) + 1
            continue
        t = (text or "")
        if t not in old_map or t not in new_map:
            skipped["某边缺卡"] = skipped.get("某边缺卡", 0) + 1
            continue
        pairs.append({"quote_id": qid, "query": q,
                      "old_targets": set(old_map[t]),
                      "new_targets": set(new_map[t])})
    print(f"可用样本 {len(pairs)} 条；跳过 {skipped}\n")

    def run(ns: str, key: str) -> dict:
        hits = {1: 0, 5: 0, 10: 0}
        rr = 0.0
        nmat = []
        lat = []
        for p in pairs:
            res = recall(p["query"], namespace=ns, settings=s)
            ids = [m.get("event_id") for m in (res.materials or [])][:10]
            nmat.append(len(res.materials or []))
            lat.append(res.elapsed_ms or 0.0)
            rank = next((i for i, e in enumerate(ids, 1) if e in p[key]), None)
            if rank:
                for k in hits:
                    if rank <= k:
                        hits[k] += 1
                rr += 1.0 / rank
        n = len(pairs)
        return {"hit@1": hits[1] / n, "hit@5": hits[5] / n, "hit@10": hits[10] / n,
                "mrr": rr / n, "nmat": statistics.mean(nmat),
                "p50": statistics.median(lat)}

    print("评测中（旧 → 新）…")
    a = run(args.old, "old_targets")
    print(f"旧 {args.old}")
    print(f"  hit@1 {a['hit@1']:.3f}  hit@5 {a['hit@5']:.3f}  hit@10 {a['hit@10']:.3f}  "
          f"MRR {a['mrr']:.3f}  材料均 {a['nmat']:.1f}  P50 {a['p50']:.0f}ms")
    b = run(args.new, "new_targets")
    print(f"新 {args.new}")
    print(f"  hit@1 {b['hit@1']:.3f}  hit@5 {b['hit@5']:.3f}  hit@10 {b['hit@10']:.3f}  "
          f"MRR {b['mrr']:.3f}  材料均 {b['nmat']:.1f}  P50 {b['p50']:.0f}ms")

    print(f"\n{'指标':<10}{'旧':>8}{'新':>8}{'Δ':>8}")
    for k in ("hit@1", "hit@5", "hit@10", "mrr"):
        print(f"{k:<10}{a[k]:>8.3f}{b[k]:>8.3f}{b[k]-a[k]:>+8.3f}")
    print(f"{'材料均':<10}{a['nmat']:>8.1f}{b['nmat']:>8.1f}"
          f"{b['nmat']-a['nmat']:>+8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
