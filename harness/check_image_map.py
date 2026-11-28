# -*- coding: utf-8 -*-
"""验证：raw_quote 里的 `[图片] <HASH>.png` 能否在 QQ 图片缓存里找到真实文件。

## 为什么查这个

用户问：如果 LLM 支持图片，能否让它结合上下文概括图片内容？
前提是**图片文件本身可得**。QQ 把图片存成 `<md5>.<ext>`，
而消息文本里是 `[图片] <MD5大写>.<ext>` —— 如果哈希就是 md5，
那就能按 `Pic/*/Ori/<md5小写>.<ext>` 精确定位。

## 输出

- 能匹配的比例
- 匹配到的文件实际大小 / 前缀
- 抽样几条消息 + 对应文件路径
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import sys

sys.path.insert(0, "/mnt/e/PycharmProjects/amarantine/back_end/src")
sys.stdout.reconfigure(encoding="utf-8")

from memory.services.local_paths import load_settings_from_project  # noqa: E402
from memory.services.raw_quote import db_path_from_settings  # noqa: E402

NS = "qq-10000000001-v2"
PIC_ROOT = pathlib.Path(
    "/mnt/d/TencentData/Chats/Tencent Files/10000000001/nt_qq/nt_data/Pic")

# `[图片] 8FDD64B8...png` 或 `[图片] xxx.jpg`
IMG_RE = re.compile(r"\[图片\]\s*([A-Za-z0-9]{16,64})\.(png|jpg|jpeg|gif|webp)",
                    re.IGNORECASE)


def build_index() -> dict[str, pathlib.Path]:
    """`md5小写 → 文件路径`（优先 Ori 原图，其次 Thumb）。"""
    idx: dict[str, pathlib.Path] = {}
    for sub in ("Ori", "Thumb"):
        for p in PIC_ROOT.glob(f"*/{sub}/*"):
            if not p.is_file():
                continue
            stem = p.stem.lower()
            # Ori 优先：先入的保留
            if sub == "Ori" or stem not in idx:
                idx[stem] = p
    return idx


def main() -> int:
    s = load_settings_from_project()
    con = sqlite3.connect(f"file:{db_path_from_settings(s)}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT quote_id, text, ts, session_id FROM raw_quote WHERE namespace=?",
        (NS,)).fetchall()
    con.close()
    print(f"原文 {len(rows)} 条")

    # 抽出所有图片引用的 md5
    refs: list[tuple[str, str, str, str]] = []  # (quote_id, text, md5, ext)
    for qid, text, ts, sid in rows:
        for m in IMG_RE.finditer(text or ""):
            refs.append((qid, text or "", m.group(1).lower(), m.group(2).lower()))
    print(f"含图片引用的消息 {len({r[0] for r in refs})} 条 / 引用 {len(refs)} 个")
    if not refs:
        print("（没有可匹配的图片引用）")
        return 0

    print(f"\n扫图片缓存 {PIC_ROOT} …")
    idx = build_index()
    print(f"缓存索引 {len(idx)} 个文件")

    hit = 0
    sizes: list[int] = []
    samples: list[tuple] = []
    for qid, text, md5, ext in refs:
        p = idx.get(md5)
        if p:
            hit += 1
            try:
                sz = p.stat().st_size
            except OSError:
                sz = 0
            sizes.append(sz)
            if len(samples) < 6:
                samples.append((text[:44], str(p)[-64:], sz))
    print(f"\n能按 md5 定位到文件：{hit}/{len(refs)}（{hit/len(refs):.1%}）")
    if sizes:
        sizes.sort()
        print(f"  文件大小：P50 {sizes[len(sizes)//2]/1024:.0f}KB  "
              f"最大 {sizes[-1]/1024/1024:.1f}MB")
    print("\n抽样：")
    for text, path, sz in samples:
        print(f"  消息: {text}")
        print(f"    文件: {path}（{sz/1024:.0f}KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
