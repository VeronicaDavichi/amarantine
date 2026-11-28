# -*- coding: utf-8 -*-
"""统计：图片消息的构成（纯图片 vs 图文混合），以及有多少是表情/贴图。

用于判断「让 LLM 看图片」的收益面有多大。
"""

from __future__ import annotations

import re
import sqlite3
import sys

sys.path.insert(0, "/mnt/e/PycharmProjects/amarantine/back_end/src")
sys.stdout.reconfigure(encoding="utf-8")

from memory.services.local_paths import load_settings_from_project  # noqa: E402
from memory.services.raw_quote import db_path_from_settings  # noqa: E402

NS = "qq-505456846-v2"
IMG_RE = re.compile(r"\[图片\]\s*[A-Za-z0-9]{16,64}\.(?:png|jpg|jpeg|gif|webp)", re.I)
OTHER_MEDIA = re.compile(r"\[(表情|视频|动画表情|语音|文件|链接)\]")


def main() -> int:
    s = load_settings_from_project()
    con = sqlite3.connect(f"file:{db_path_from_settings(s)}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT text FROM raw_quote WHERE namespace=?", (NS,)).fetchall()
    con.close()

    total = len(rows)
    img_msgs = [t or "" for (t,) in rows if IMG_RE.search(t or "")]
    pure_img, mixed = 0, 0
    with_emoji = 0
    text_lengths_pure: list[int] = []
    for t in img_msgs:
        rest = IMG_RE.sub("", t)
        rest = re.sub(r"^[^：:]{1,24}[：:]\s*", "", rest)  # 去说话人前缀
        rest = rest.strip()
        if not rest:
            pure_img += 1
            text_lengths_pure.append(0)
        else:
            mixed += 1
        if OTHER_MEDIA.search(t):
            with_emoji += 1

    print(f"原文总数            {total}")
    print(f"含图片的消息         {len(img_msgs)}（{len(img_msgs)/total:.1%}）")
    print(f"  · 纯图片（无文字）  {pure_img}")
    print(f"  · 图文混合          {mixed}")
    print(f"  · 同条还含表情/视频 {with_emoji}")

    only_emoji = sum(1 for (t,) in rows
                     if OTHER_MEDIA.search(t or "") and not IMG_RE.search(t or ""))
    print(f"\n只含表情/视频/文件、无图片  {only_emoji}")

    print(f"\n纯图片消息的上下文价值判断：")
    print(f"  {pure_img} 条纯图片没有文字线索 —— 这些正是此前「没进任何卡」的 138 条的主要来源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
