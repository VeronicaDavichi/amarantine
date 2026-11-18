#!/usr/bin/env python3
"""用人工标注的 10 条陈述验证改造 A（LLM + 实体约束）。

对比口径：
- 人工给了 head/tail/intensity → 算「有关系」
- 机器抽出 (head, predicate, tail) → 用 intensity 反查谓词
- 命中判定：intensity 相同 + head/tail 互相包含（容忍 `老师` vs `老师五点`）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

from memory.config import load_settings  # noqa: E402
from memory.services.relations import (  # noqa: E402
    PREDICATE_TO_INTENSITY,
    extract_statements_combined,
)

DATA = ROOT / "docs" / "labeling-data.js"
LABELS = ROOT / "back_end" / "data" / "labeling-export.json"


def load_data() -> dict:
    text = DATA.read_text(encoding="utf-8")
    return json.loads(text[len("window.LABEL_DATA = "):].rstrip().rstrip(";"))



#: 备注里的关系词 → 强度（与标注页 0–4 一致）
_NOTE_INTENSITY = {
    "强相关": 4, "负责": 4, "拥有": 4, "OWNS": 4,
    "维护": 3, "部署": 3, "对接": 3, "MAINTAINS": 3,
    "有关系": 2, "协作": 2, "一起": 2, "COLLABORATES": 2,
    "依赖": 1, "DEPENDS": 1,
}

_NOTE_PAT = re.compile(
    r"(?P<head>[^\s，,；;。:：]{2,12})\s*"
    r"(?P<rel>强相关|负责|拥有|维护|部署|对接|有关系|协作|一起|依赖)\s*"
    r"(?P<tail>[^\s，,；;。:：]{2,12})"
)


def parse_note_relations(note: str) -> list[tuple[str, str, int]]:
    """把备注里写的关系解析成 `[(head, tail, intensity)]`。

    备注是用户手写的自由文本，例：
    `任泽昂 有关系 老师们         任泽昂 负责 方案          老师们 维护 方案`
    解析不出来的片段直接跳过 —— 宁可少算真值，也不要把噪声当真值。
    """
    out: list[tuple[str, str, int]] = []
    for m in _NOTE_PAT.finditer(note or ""):
        head = m.group("head").strip()
        tail = m.group("tail").strip()
        inten = _NOTE_INTENSITY.get(m.group("rel"), 0)
        if head and tail and head != tail and inten:
            out.append((head, tail, inten))
    return out


def norm(s: str | None) -> str:
    return "".join(str(s or "").split())


def match(a: str, b: str) -> bool:
    x, y = norm(a), norm(b)
    return bool(x and y) and (x == y or x in y or y in x)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(LABELS))
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    data = load_data()
    labels = json.loads(pathlib.Path(args.labels).read_text(encoding="utf-8"))["labels"]
    settings = load_settings()

    # 名册：从身份文件取（真实链路是图里的 Person）
    roster: list[str] = []
    for f in (ROOT / "back_end" / "data" / "corpus" / "qq-identities").glob("group-*.json"):
        try:
            members = json.loads(f.read_text(encoding="utf-8")).get("members") or []
        except Exception:
            continue
        for m in members:
            for raw in (m.get("nickname"), m.get("card")):
                name = str(raw or "").strip()
                if 2 <= len(name) <= 12:
                    roster.append(name)
    roster = sorted(set(roster))

    tp = fp = fn = tn = 0
    for item in data.get("statements", []):
        sid = item["id"]
        text = item.get("evidence") or ""
        lab = labels.get(sid) or {}
        human_int = lab.get("intensity")
        human_head = (lab.get("head") or {}).get("name")
        human_tail = (lab.get("tail") or {}).get("name")

        claims, rejected = extract_statements_combined(
            text, settings, roster=roster, people=roster,
            use_llm=not args.no_llm)
        got_int = [PREDICATE_TO_INTENSITY.get(c.predicate) for c in claims]

        human_rel = human_int is not None and human_int > 0
        # 真值 = 字段给出的 (head, tail, intensity) + 备注里写的关系
        truth: list[tuple[str | None, str | None, int]] = []
        if human_rel:
            truth.append((human_head, human_tail, human_int))
        truth.extend(parse_note_relations(lab.get("note") or ""))

        # 人工若显式选了谓词，以它为准（强度只是「几分」，谓词是「哪种关系」）
        human_pred = str(lab.get("predicate") or "").strip()
        hit = False
        if claims:
            for c in claims:
                ci = PREDICATE_TO_INTENSITY.get(c.predicate)
                for th, tt, ti in truth:
                    if human_pred:
                        if c.predicate != human_pred:
                            continue
                    elif ci != ti:
                        continue
                    head_ok = match(c.head, th) if th else True
                    tail_ok = match(c.tail, tt) if tt else True
                    if head_ok and tail_ok:
                        hit = True
                        break
                if hit:
                    break
        if human_rel and hit:
            tp += 1
        elif human_rel and not hit:
            fn += 1
            if claims:
                fp += 1
        elif not human_rel:
            if claims:
                fp += 1
            else:
                tn += 1

        print(f"\n{sid} | 人工 intensity={human_int} "
              f"谓词={human_pred or '（按强度）'} "
              f"({human_head or '?'} → {human_tail or '?'})")
        if truth:
            print(f"  真值(含备注): {truth}")
        print(f"  原文: {text[:70]}")
        if claims:
            for c in claims:
                ci = PREDICATE_TO_INTENSITY.get(c.predicate)
                print(f"  机器[{c.source}/i{ci}]: {c.head} --{c.predicate}--> {c.tail}")
        else:
            print("  机器: （无）")
        if rejected:
            print(f"  被拒 {len(rejected)}: "
                  f"{[(r.get('head'), r.get('tail'), r.get('reason')) for r in rejected[:3]]}")
        print(f"  → {'TP' if human_rel and hit else ('FN' if human_rel else ('FP' if claims else 'TN'))}")

    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    print(f"\n=== 改造 A 结果（LLM={'关' if args.no_llm else '开'}）===")
    print(f"TP={tp} FP={fp} FN={fn} TN={tn}")
    print(f"precision={prec:.2f}  recall={rec:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
