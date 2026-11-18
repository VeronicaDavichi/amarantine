#!/usr/bin/env python3
"""关系覆盖（039 recall 口径）评分。

人工给的是「这句话实际有几条稳定关系」（0–3），机器给了 N 条。
在「机器抽到的边都正确」这一前提下（已由 20 条机器边核对确认）：

    recall（上限口径）= Σ min(机器条数, 人工条数) / Σ 人工条数

这是**上限**：万一机器某条抽错了，真实 recall 更低。所以报告里同时给
「机器少抽的题数」与「机器多抽的题数」，避免只看一个数掩盖问题。

用法：

    python3 harness/score_coverage.py --labels back_end/data/labeling-export.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "labeling-data.js"


def load_data() -> dict:
    text = DATA.read_text(encoding="utf-8")
    return json.loads(text[len("window.LABEL_DATA = "):].rstrip().rstrip(";"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    args = ap.parse_args()

    data = load_data()
    labels = json.loads(pathlib.Path(args.labels).read_text(encoding="utf-8"))["labels"]
    items = data.get("coverage") or []
    if not items:
        print("数据里没有 coverage 条目（生成时加 --coverage）")
        return 1

    hit = human_total = machine_total = 0
    exact = under = over = unlabeled = 0
    rows = []
    for it in items:
        lb = labels.get(it["id"]) or {}
        h = lb.get("intensity")
        if h is None:
            unlabeled += 1
            continue
        m = len(it.get("machine") or [])
        machine_total += m
        human_total += h
        hit += min(m, h)
        if m == h:
            exact += 1
        elif m < h:
            under += 1
        else:
            over += 1
        rows.append((it["id"], m, h, lb.get("note") or "", it["evidence"][:40]))

    done = len(rows)
    print(f"已标 {done} / {len(items)}" + (f"（未标 {unlabeled}）" if unlabeled else ""))
    if not done:
        return 1

    recall = hit / human_total if human_total else 0.0
    print("\n=== 关系覆盖（recall 上限口径）===")
    print(f"  人工认为应有关联 {human_total} 条")
    print(f"  机器抽出          {machine_total} 条")
    print(f"  命中（取小）      {hit} 条")
    print(f"  recall(上限)      {recall:.2f}")
    print(f"\n  条数完全一致 {exact} / {done}")
    print(f"  机器少抽     {under} 题")
    print(f"  机器多抽     {over} 题")

    print("\n逐条（机器/人工）：")
    for cid, m, h, note, ev in rows:
        flag = "✓" if m == h else ("少抽" if m < h else "多抽")
        print(f"  {cid:<8} {m}/{h}  {flag:<4} {ev}")
        if note:
            print(f"           备注：{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
