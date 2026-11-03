#!/usr/bin/env python3
"""A14：从事件卡重建向量（派生层重建）。

**为什么需要**：向量只经写入路径产生。换 embedding 模型（A25 会拒绝写入）
或索引损坏时，没有「重新 embed 全部卡」的入口。

用法（仓库根目录）：

    # 先看会重算多少 + 抽样验证（默认，不写）
    ./harness/dev-linux.sh python ../harness/rebuild_vectors.py --namespace work

    # 真写
    ./harness/dev-linux.sh python ../harness/rebuild_vectors.py --namespace work --apply

**默认 dry-run**：先重算前 N 条并校验维度，确认能算得出来，再决定写。

A25 联动：重建前会校验索引模型与当前配置一致。不一致直接拒绝 ——
否则重建会把两种模型的向量混进同一个索引（静默错误）。

退出码：0 = 成功，1 = 有失败项。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402
from memory.services.embedding import embed_one  # noqa: E402
from memory.services.embedding_guard import (  # noqa: E402
    EmbeddingIndexMeta,
    EmbeddingModelMismatchError,
)
from memory.services.raw_quote import db_path_from_settings  # noqa: E402
from memory.services.weaviate_store import WeaviateStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="从事件卡重建向量（A14）")
    ap.add_argument("--namespace", required=True, help="记忆域")
    ap.add_argument("--apply", action="store_true", help="真的写向量；不加则只报告")
    ap.add_argument(
        "--limit", type=int, default=0, help="只处理前 N 条（0 = 全部，便于小步试）"
    )
    args = ap.parse_args()

    settings = load_settings()

    # A25：模型不一致时绝不允许重建 —— 会把两个空间的向量混进同一索引
    meta = EmbeddingIndexMeta(db_path_from_settings(settings))
    try:
        meta.ensure_model(args.namespace, settings.EMBEDDING_MODEL)
    except EmbeddingModelMismatchError as exc:
        print(f"FATAL {exc}", file=sys.stderr)
        print(
            "\n换模型后必须先在 Weaviate 里删掉旧 collection 再重建，"
            "不能直接往旧索引里写新向量（维度可能相同、空间完全不同）。",
            file=sys.stderr,
        )
        return 1

    store = WeaviateStore(settings)
    try:
        cards = store.list_all(args.namespace)
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL 读取事件卡失败: {exc}", file=sys.stderr)
        return 1

    if args.limit > 0:
        cards = cards[: args.limit]

    if not cards:
        print("没有事件卡 —— 空库重建是幂等的（不报错，0 条）")
        store.close()
        return 0

    print(f"命名空间      : {args.namespace}")
    print(f"模型          : {settings.EMBEDDING_MODEL}")
    print(f"事件卡        : {len(cards)} 条")
    print(f"模式          : {'APPLY（真写）' if args.apply else 'DRY-RUN（不写）'}")

    done = 0
    failed: list[tuple[str, str]] = []

    try:
        for i, card in enumerate(cards, start=1):
            eid = card["event_id"]
            summary = card.get("summary") or ""
            if not summary.strip():
                failed.append((eid, "summary 为空，无法重算"))
                continue
            try:
                vector = embed_one(summary, settings)
                if not isinstance(vector, list) or len(vector) != settings.EMBEDDING_DIM:
                    got = len(vector) if isinstance(vector, list) else type(vector).__name__
                    failed.append((eid, f"维度 {got} != {settings.EMBEDDING_DIM}"))
                    continue
                if args.apply:
                    store.update_vector(args.namespace, eid, vector)
                done += 1
            except Exception as exc:  # noqa: BLE001
                failed.append((eid, str(exc)[:120]))

            if i % 50 == 0:
                print(f"  ... {i}/{len(cards)}")
    finally:
        store.close()

    total = len(cards)
    rate = done / total * 100 if total else 0.0
    print(f"\n成功 {done}/{total}（{rate:.1f}%）")
    if failed:
        print(f"失败 {len(failed)} 条，前 5 条：")
        for eid, reason in failed[:5]:
            print(f"  - {eid}: {reason}")
        return 1

    if not args.apply:
        print("\n加 --apply 才会真的写。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
