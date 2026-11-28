# -*- coding: utf-8 -*-
"""深挖：为什么 99% 的图片引用匹配不上缓存？

对几个未匹配的 md5，在 Pic 全树里搜（不限子目录），看是否存在或格式不同。
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
PIC = pathlib.Path("/mnt/d/TencentData/Chats/Tencent Files/10000000001/nt_qq/nt_data/Pic")
IMG_RE = re.compile(r"\[图片\]\s*([A-Za-z0-9]{16,64})\.(png|jpg|jpeg|gif|webp)", re.I)


def main() -> int:
    s = load_settings_from_project()
    con = sqlite3.connect(f"file:{db_path_from_settings(s)}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT text, ts FROM raw_quote WHERE namespace=? AND text LIKE '%[图片]%'",
        (NS,)).fetchall()
    con.close()

    all_md5 = set()
    for text, _ts in rows:
        for m in IMG_RE.finditer(text or ""):
            all_md5.add(m.group(1).lower())
    print(f"消息里出现的图片 md5：{len(all_md5)} 个（去重）")

    # 建成一个「文件名 stem → 路径」的全树索引（含所有子目录）
    stems: dict[str, pathlib.Path] = {}
    total = 0
    for p in PIC.rglob("*"):
        if p.is_file():
            total += 1
            stems.setdefault(p.stem.lower(), p)
    print(f"Pic 全树文件 {total} 个")

    matched = all_md5 & set(stems)
    print(f"能匹配：{len(matched)}/{len(all_md5)}（{len(matched)/max(1,len(all_md5)):.1%}）")

    miss = sorted(all_md5 - set(stems))[:8]
    print(f"\n未匹配样例（前 8）：")
    for m in miss:
        print(f"  {m}")

    # 反向：缓存里有但消息里没有的 —— 说明缓存装的是别的图
    extra = list(set(stems) - all_md5)
    print(f"\n缓存里有、但消息里没出现的：{len(extra)} 个")
    print("  （如果这个数很大，说明缓存里大部分图不是这批消息的）")

    # 抽样看未匹配的那些月份分布
    print("\n未匹配哈希对应的消息时间分布：")
    import collections

    by_month: collections.Counter[str] = collections.Counter()
    hit_month: collections.Counter[str] = collections.Counter()
    for text, ts in rows:
        for m in IMG_RE.finditer(text or ""):
            month = str(ts or "")[:7]
            if m.group(1).lower() in stems:
                hit_month[month] += 1
            else:
                by_month[month] += 1
    for month in sorted(set(list(by_month) + list(hit_month))):
        print(f"  {month}  未匹配 {by_month.get(month,0):>5}  匹配 {hit_month.get(month,0):>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
