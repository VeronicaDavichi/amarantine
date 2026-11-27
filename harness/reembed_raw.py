#!/usr/bin/env python3
"""把存量卡的向量从 summary 重算成原文窗口（spec 052）。

## 为什么必须全量

同一个 collection 里如果一部分卡向量来自 summary、另一部分来自原文，
两种向量语义空间不同、**相似度不可比**，排序会失真。所以「只算一部分」
在这条路上不成立 —— 要么全换，要么不换。

## 成本（实测）

- 卡级原文约 780 字 → bge-m3 CPU 上 **2.5 秒/卡**
- 4383 张 ≈ **3 小时**

## 幂等与续跑

向量是覆盖写，跑两遍结果相同。中断后重跑会跳过已经写过的
（用 `--skip-done` + 本地进度文件），避免 3 小时白干。

## 用法

    python3 harness/reembed_raw.py --namespace qq-10000000001            # 预演
    python3 harness/reembed_raw.py --namespace qq-10000000001 --apply    # 真写
    python3 harness/reembed_raw.py ... --apply --limit 50                 # 小批试
    python3 harness/reembed_raw.py ... --apply --resume                   # 续跑
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

#: 进度文件（记住哪些 event_id 已重算）
PROGRESS = pathlib.Path("/tmp/reembed_raw_progress.json")


def load_progress() -> set[str]:
    if PROGRESS.is_file():
        try:
            return set(json.loads(PROGRESS.read_text()))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def save_progress(done: set[str]) -> None:
    PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="重算存量为原文向量（spec 052）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true", help="真写（默认预演）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张")
    ap.add_argument("--resume", action="store_true", help="跳过进度文件里已完成的")
    ap.add_argument("--max-chars", type=int, default=0, help="向量文本上限（0=默认）")
    args = ap.parse_args()

    from memory.services import raw_text_index as R
    from memory.services.embedding import embed_one
    from memory.services.local_paths import load_settings_from_project
    from memory.services.raw_quote import RawQuoteStore, db_path_from_settings
    from memory.services.weaviate_store import COLLECTION, deterministic_uuid

    settings = load_settings_from_project()
    ns = args.namespace
    max_chars = args.max_chars or R.VECTOR_MAX_CHARS

    import urllib.request

    base = str(settings.WEAVIATE_URL).rstrip("/")

    def gql(q: str) -> dict:
        req = urllib.request.Request(
            f"{base}/v1/graphql", data=json.dumps({"query": q}).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=300).read())

    # 1) 拉卡
    cards: list[dict] = []
    offset = 0
    while True:
        d = gql('{ Get { ' + COLLECTION + '(where: {path:["namespace"], operator: Equal, '
                'valueText: "' + ns + '"}, limit: 500, offset: ' + str(offset) + ') '
                '{ event_id summary raw_quote_ids } } }')
        rows = ((d.get("data") or {}).get("Get") or {}).get(COLLECTION) or []
        if not rows:
            break
        cards.extend(rows)
        if len(rows) < 500:
            break
        offset += 500
    if args.limit:
        cards = cards[: args.limit]
    print(f"卡片 {len(cards)} 张（namespace={ns}）")

    done = load_progress() if args.resume else set()
    if done:
        cards = [c for c in cards if c["event_id"] not in done]
        print(f"  跳过已完成 {len(done)} 张，剩 {len(cards)} 张")

    raw_store = RawQuoteStore(db_path_from_settings(settings))
    stats = {"raw": 0, "summary": 0, "failed": 0, "written": 0}
    t0 = time.monotonic()

    for i, c in enumerate(cards, 1):
        eid = c["event_id"]
        qids = c.get("raw_quote_ids") or []
        try:
            texts = raw_store.texts_for(ns, qids) if qids else []
            vtext, src = R.build_vector_text(texts, fallback=c.get("summary") or "",
                                            max_chars=max_chars)
            if not vtext.strip():
                stats["failed"] += 1
                continue
            vec = embed_one(vtext, settings)
            stats[src] += 1
            if args.apply:
                uid = deterministic_uuid(ns, eid)
                # 必须 PATCH：PUT 会报 "field 'id' is immutable"（实测 422）
                req = urllib.request.Request(
                    f"{base}/v1/objects/{COLLECTION}/{uid}",
                    data=json.dumps({"class": COLLECTION, "vector": vec}).encode(),
                    headers={"Content-Type": "application/json"}, method="PATCH")
                urllib.request.urlopen(req, timeout=60)
                stats["written"] += 1
                done.add(eid)
        except Exception as exc:  # noqa: BLE001 - 单卡失败跳过
            stats["failed"] += 1
            if stats["failed"] <= 3:
                print(f"  ! {eid}: {type(exc).__name__}: {exc}")

        if i % 50 == 0:
            el = time.monotonic() - t0
            rate = el / i
            print(f"  ...{i}/{len(cards)}  已用 {el/60:.1f} 分钟  "
                  f"预计还需 {rate*(len(cards)-i)/60:.1f} 分钟  "
                  f"raw={stats['raw']} summary={stats['summary']} "
                  f"failed={stats['failed']}", flush=True)
            if args.apply:
                save_progress(done)

    if args.apply:
        save_progress(done)

    print(f"\n{'指标':<12}{'值':>10}")
    print(f"{'卡数':<12}{len(cards):>10}")
    print(f"{'用原文向量':<12}{stats['raw']:>10}")
    print(f"{'退回摘要':<12}{stats['summary']:>10}")
    print(f"{'失败':<12}{stats['failed']:>10}")
    print(f"{'已写入':<12}{stats['written']:>10}")
    print(f"{'耗时':<12}{(time.monotonic()-t0)/60:>9.1f} 分")
    if not args.apply:
        print("\n（预演；加 --apply 才写）")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
