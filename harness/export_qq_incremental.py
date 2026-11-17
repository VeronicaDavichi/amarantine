#!/usr/bin/env python3
"""增量导出：按每个群**自己**的最后消息时间起导（spec 040 的日常用法）。

为什么不统一用一个 `--since`：12 个群的最新消息时间差 8 个月。
用最早的当起点，大群会被重复拉几万条；用最晚的，其他群的新消息全漏。
每个群各自起导才既省又全。

用法：

    python3 harness/export_qq_incremental.py --list          # 只列出起点，不导出
    python3 harness/export_qq_incremental.py                 # 导出到 harness/corpus/qq-inc/
    python3 harness/export_qq_incremental.py --overlap-days 1
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "back_end" / "data" / "memory.db"
OUT = ROOT / "harness" / "corpus" / "qq-inc"
QQ_DB = pathlib.Path(
    "/mnt/d/TencentData/Chats/Tencent Files/505456846/nt_qq/nt_db/nt_msg.db")
NAMESPACE = "qq-505456846"


def last_times(db: pathlib.Path, namespace: str) -> dict[str, str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT session_id, MAX(ts) AS hi, COUNT(*) AS n FROM raw_quote "
        "WHERE namespace=? GROUP BY session_id", (namespace,)).fetchall()
    con.close()
    out: dict[str, str] = {}
    for r in rows:
        sid = str(r["session_id"] or "").replace("qq-", "")
        if sid and r["hi"]:
            out[sid] = str(r["hi"])
    return out


def parse_ts(ts: str) -> datetime:
    text = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> int:
    ap = argparse.ArgumentParser(description="按群增量导出 QQ 聊天")
    ap.add_argument("--list", action="store_true", help="只打印起点")
    ap.add_argument("--db", default=str(DB), help="memory.db 路径")
    ap.add_argument("--qq-db", default=str(QQ_DB))
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--overlap-hours", type=int, default=2,
                    help="从最后时间往前多导几小时（默认 2，防同一分钟漏消息）")
    ap.add_argument("--limit", type=int, default=5000, help="每群最多导出条数")
    args = ap.parse_args()

    if not pathlib.Path(args.qq_db).is_file():
        print(f"✗ QQ 库不存在：{args.qq_db}")
        return 1

    lt = last_times(pathlib.Path(args.db), NAMESPACE)
    if not lt:
        print("没有已导会话")
        return 1

    plan: list[tuple[str, int, str]] = []
    for gid, ts in sorted(lt.items(), key=lambda kv: kv[1], reverse=True):
        since = parse_ts(ts) - timedelta(hours=args.overlap_hours)
        epoch = int(since.timestamp())
        local = since.astimezone(timezone(timedelta(hours=8)))
        plan.append((gid, epoch, local.strftime("%Y-%m-%d %H:%M")))

    print(f"{'群号':<14}{'起点(UTC+8)':<20}")
    for gid, _e, local in plan:
        print(f"{gid:<14}{local:<20}")

    if args.list:
        return 0

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    groups_arg = ",".join(g for g, _e, _l in plan)
    since_arg = str(min(e for _g, e, _l in plan))  # 仅用于日志；真正按群各导

    written: list[pathlib.Path] = []
    for gid, epoch, local in plan:
        out = out_dir / f"qq-{gid}.json"
        cmd = [
            "node", str(ROOT / "harness" / "export_qq.mjs"),
            "--db", args.qq_db,
            "--groups", gid,
            "--per-group-limit", str(args.limit),
            "--out-dir", str(out_dir),
            "--since", str(epoch),
        ]
        print(f"\n=== {gid} | 起 {local} ===")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        tail = (proc.stdout or "").strip().splitlines()[-4:]
        for line in tail:
            print(f"  {line}")
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()[-3:]
            print(f"  ✗ 失败: {err}")
            continue
        if out.is_file():
            data = json.loads(out.read_text(encoding="utf-8"))
            msgs = data.get("messages") or data.get("turns") or []
            print(f"  → {len(msgs)} 条")
            written.append(out)

    print(f"\n完成 {len(written)}/{len(plan)} 个群；groups={groups_arg} since_min={since_arg}")
    print(f"语料目录：{out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
