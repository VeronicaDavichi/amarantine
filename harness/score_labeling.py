#!/usr/bin/env python3
"""计算人工标注的 precision / recall（spec 039 / 041 的 T6 / T8）。

用法：

    python3 harness/score_labeling.py --labels ../back_end/data/labeling-export.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.services import relations as R  # noqa: E402

DATA = ROOT / "docs" / "labeling-data.js"


def load_data() -> dict:
    text = DATA.read_text(encoding="utf-8")
    prefix = "window.LABEL_DATA = "
    return json.loads(text[len(prefix):].rstrip().rstrip(";"))


def norm(s: str | None) -> str:
    return "".join(str(s or "").split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    args = ap.parse_args()

    data = load_data()
    labels = json.loads(pathlib.Path(args.labels).read_text(encoding="utf-8"))["labels"]

    # ---------------- 陈述 ----------------
    stmts = {x["id"]: x for x in data.get("statements", [])}
    tp = fp = fn = tn = 0
    details = []
    for sid, item in stmts.items():
        lab = labels.get(sid) or {}
        intensity = lab.get("intensity")
        machine = item.get("machine") or {}
        extracted = bool(machine.get("extracted"))
        # 人工给出的关系：intensity 有值且 > 0
        human_relation = intensity is not None and intensity > 0
        # 机器这条边是否与人工一致：主语/宾语都要对上
        same = False
        if extracted and human_relation:
            mh, mt = norm(machine.get("head")), norm(machine.get("tail"))
            uh = norm((lab.get("head") or {}).get("name"))
            ut = norm((lab.get("tail") or {}).get("name"))
            same = bool(mh and mt and uh and ut and (mh == uh) and (mt == ut))
        if human_relation:
            if same:
                tp += 1
            else:
                fn += 1
            if extracted and not same:
                fp += 1
        else:
            if extracted:
                fp += 1
            else:
                tn += 1
        details.append((sid, extracted, human_relation, same, intensity))

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0

    print("=== 关系陈述（039 T6）===")
    print(f"  TP={tp} FP={fp} FN={fn} TN={tn}")
    print(f"  precision = {precision:.2f}   recall = {recall:.2f}")
    for d in details:
        print(f"    {d[0]:<12} 机器抽={d[1]!s:<5} 人工有关系={d[2]!s:<5} 一致={d[3]!s:<5} intensity={d[4]}")

    # ---------------- 行为 ----------------
    print("\n=== 关系行为（039 T6 行为通道）===")
    behs = {x["id"]: x for x in data.get("behaviors", [])}
    stable = labeled = 0
    for bid, item in behs.items():
        lab = labels.get(bid) or {}
        inten = lab.get("intensity")
        if inten is None:
            continue
        labeled += 1
        if inten >= 2:
            stable += 1
    print(f"  已标 {labeled}/{len(behs)}；人为「有关系」(≥2) {stable} 条")
    print("  （行为通道没有「机器是否建边」的对照，这里只统计分布）")

    # ---------------- 义项 ----------------
    print("\n=== 指称义项（041 T8）===")
    senses = {x["id"]: x for x in data.get("senses", [])}
    ok = bad = unk = 0
    for sid in senses:
        lab = labels.get(sid) or {}
        if lab.get("label") == "ok":
            ok += 1
        elif lab.get("label") == "bad":
            bad += 1
        elif lab.get("label") == "warn":
            unk += 1
    total = ok + bad
    acc = ok / total if total else 0.0
    print(f"  ok={ok} bad={bad} 不确定={unk} → 正确率 = {acc:.2f}")

    # ---------------- 谓词分布 ----------------
    print("\n=== 人工强度分布（陈述，0–4）===")
    dist: dict[int, int] = {}
    for sid in stmts:
        inten = (labels.get(sid) or {}).get("intensity")
        if inten is not None:
            dist[inten] = dist.get(inten, 0) + 1
    for k in sorted(dist):
        name = {0: "无关系", 1: "依赖", 2: "协作", 3: "维护", 4: "负责"}.get(k, str(k))
        print(f"  {k} {name}: {dist[k]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
