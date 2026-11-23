#!/usr/bin/env python3
"""T4：把存量卡片的原文分词回填进 Weaviate（spec 051）。

## 为什么手动跑

4383 张卡 × 每张 30 条原文的拼接 + PATCH 会拖慢冷启动；失败还会让人以为
服务坏了。手动脚本 + `/health` 的计数提示更可控（与 `CHUNK_ENABLED` 同思路）。

## 幂等

按 `event_id` 覆盖写同一字段。重复跑结果相同 —— 中途断了重跑即可。

用法：

    python3 harness/reindex_raw_text.py --namespace qq-10000000001            # 预演
    python3 harness/reindex_raw_text.py --namespace qq-10000000001 --apply    # 写
    python3 harness/reindex_raw_text.py --namespace x --apply --limit 50      # 小批试
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))


def fetch_cards(namespace: str, settings, *, limit: int | None) -> list[dict]:
    """拉 `(event_id, raw_quote_ids)`。分页，避免一次拉爆。"""
    import httpx

    base = str(settings.WEAVIATE_URL).rstrip("/")
    out: list[dict] = []
    offset = 0
    page = 200
    while True:
        gql = (
            '{ Get { MemoryEvent(where: {path:["namespace"], operator: Equal, '
            f'valueText:"{namespace}"}}, limit: {page}, offset: {offset}) '
            "{ event_id raw_quote_ids } } }"
        )
        r = httpx.post(f"{base}/v1/graphql", json={"query": gql}, timeout=180).json()
        if r.get("errors"):
            raise SystemExit(f"GraphQL 错: {r['errors'][:1]}")
        rows = ((r.get("data") or {}).get("Get") or {}).get("MemoryEvent") or []
        if not rows:
            break
        out.extend(rows)
        if limit and len(out) >= limit:
            return out[:limit]
        if len(rows) < page:
            break
        offset += page
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="回填原文分词索引（spec 051）")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--apply", action="store_true", help="真写（默认预演）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张（试跑用）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from memory.services.local_paths import load_settings_from_project
    from memory.services import raw_text_index as R
    from memory.services.raw_quote import RawQuoteStore, db_path_from_settings
    from memory.services.weaviate_store import WeaviateStore

    settings = load_settings_from_project()
    ns = args.namespace
    cards = fetch_cards(ns, settings, limit=args.limit or None)
    print(f"卡片 {len(cards)} 张")

    raw_store = RawQuoteStore(db_path_from_settings(settings))
    store = WeaviateStore(settings)
    store.ensure_collection()  # 触发增量补属性（raw_text_tokens）

    max_chars = R.index_max_chars(settings)
    stats = {"cards": 0, "with_tokens": 0, "empty": 0, "errors": 0,
             "chars_in": 0, "tokens": 0, "gated_quotes": 0, "total_quotes": 0}
    written = 0
    for i, c in enumerate(cards, 1):
        eid = c.get("event_id")
        qids = c.get("raw_quote_ids") or []
        if not eid:
            continue
        stats["cards"] += 1
        stats["total_quotes"] += len(qids)
        try:
            texts = raw_store.texts_for(ns, qids)
            stats["gated_quotes"] += len(qids) - len(R.filter_quotes(texts))
            tokens = R.build_tokens(texts, max_chars=max_chars)
            if tokens:
                stats["with_tokens"] += 1
                stats["tokens"] += len(tokens.split())
                stats["chars_in"] += len("".join(R.filter_quotes(texts)))
            else:
                stats["empty"] += 1
            if args.apply:
                store.set_raw_text_tokens(eid, ns, tokens=tokens)
                written += 1
        except Exception as exc:  # noqa: BLE001 - 单张失败继续
            stats["errors"] += 1
            if stats["errors"] <= 3:
                print(f"  ⚠ {eid}: {type(exc).__name__}: {exc}")
        if i % 500 == 0:
            print(f"  ...{i}/{len(cards)}（已写 {written}）")

    if args.json:
        print(json.dumps({**stats, "written": written, "applied": args.apply},
                         ensure_ascii=False, indent=1))
        return 0

    print(f"\n{'指标':<16}{'值':>10}")
    print(f"{'卡片':<16}{stats['cards']:>10}")
    print(f"{'写了 token':<16}{stats['with_tokens']:>10}")
    print(f"{'全被闸挡掉':<16}{stats['empty']:>10}"
          f"   ({stats['empty']/max(1,stats['cards']):.1%})")
    print(f"{'出错':<16}{stats['errors']:>10}")
    print(f"{'原文条数':<16}{stats['total_quotes']:>10}")
    print(f"{'被闸挡掉条数':<16}{stats['gated_quotes']:>10}"
          f"   ({stats['gated_quotes']/max(1,stats['total_quotes']):.2%})")
    print(f"{'入索引字符':<16}{stats['chars_in']:>10}")
    print(f"{'token 数':<16}{stats['tokens']:>10}")
    print(f"\n{'已写入' if args.apply else '（预演，未写）':<16}{written:>10}")
    if args.apply:
        idx, total = store.count_raw_text_indexed(ns)
        print(f"复核：已索引 {idx} / 总 {total}")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
