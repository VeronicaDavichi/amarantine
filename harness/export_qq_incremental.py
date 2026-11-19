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
import os
import pathlib
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "back_end" / "data" / "memory.db"
OUT = ROOT / "harness" / "corpus" / "qq-inc"
#: 真实路径与账号**不进代码**（开源仓库里是占位符），用环境变量给：
#:   QQ_NT_MSG_DB=/mnt/d/.../nt_msg.db  AMARANTINE_NAMESPACE=qq-你的号
QQ_DB = pathlib.Path(os.environ.get("QQ_NT_MSG_DB", ""))
NAMESPACE = os.environ.get("AMARANTINE_NAMESPACE", "qq-10000000001")
INACTIVE = ROOT / "back_end" / "config" / "inactive_sessions.json"


def inactive_map(namespace: str) -> dict[str, dict]:
    """读不活跃群清单：`{gid: {active, last_message, reason}}`。

    为什么要这个文件：死群每次增量都会白跑一遍导出（要起 MCP 会话、查库、写文件），
    而结果是 0 条新消息。把它们记下来，默认跳过，需要时 `--include-inactive`。
    """
    if not INACTIVE.is_file():
        return {}
    try:
        data = json.loads(INACTIVE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 清单坏了不该阻断导出
        print(f"⚠ 读不活跃清单失败（忽略）: {exc}")
        return {}
    ns = data.get(namespace)
    if ns is None:
        # 开源版清单里的 namespace 是占位符（`qq-10000000001`），
        # 用户本地真实 namespace 不同。只有一份清单时就用它 ——
        # 否则「不活跃群」功能会在真实环境里静默失效。
        buckets = [v for k, v in data.items()
                   if not k.startswith("_") and isinstance(v, dict)]
        if len(buckets) == 1:
            ns = buckets[0]
        else:
            return {}
    return {str(k): v for k, v in ns.items() if isinstance(v, dict)}


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
    ap.add_argument("--keep-plain", action="store_true",
                    help="导出后**保留** qq-mcp 的解密副本（默认用完就删）")
    ap.add_argument("--include-inactive", action="store_true",
                    help="连不活跃群一起导（默认跳过）")
    ap.add_argument("--mark-inactive", action="store_true",
                    help="把本次导到 0 条新消息的群写进不活跃清单")
    args = ap.parse_args()

    if not pathlib.Path(args.qq_db).is_file():
        print(f"✗ QQ 库不存在：{args.qq_db}")
        return 1

    lt = last_times(pathlib.Path(args.db), NAMESPACE)
    if not lt:
        print("没有已导会话")
        return 1

    dead = inactive_map(NAMESPACE)
    skipped = []
    if not args.include_inactive:
        for gid in list(lt):
            info = dead.get(gid) or {}
            if info.get("active") is False:
                skipped.append((gid, info.get("last_message") or "-",
                                info.get("reason") or ""))
                lt.pop(gid)

    plan: list[tuple[str, int, str]] = []
    for gid, ts in sorted(lt.items(), key=lambda kv: kv[1], reverse=True):
        since = parse_ts(ts) - timedelta(hours=args.overlap_hours)
        epoch = int(since.timestamp())
        local = since.astimezone(timezone(timedelta(hours=8)))
        plan.append((gid, epoch, local.strftime("%Y-%m-%d %H:%M")))

    if skipped:
        print(f"跳过不活跃群 {len(skipped)} 个（--include-inactive 可导）：")
        for gid, last, reason in skipped:
            print(f"  - {gid}  最后消息 {last[:10]}  {reason}")
        print()
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
    fresh: dict[str, int] = {}   # gid → 严格晚于上次最大的消息数
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
            # 「新」= 严格晚于上次的最大时间；边界重叠那几条不算
            prev = lt.get(gid)
            new_n = 0
            if prev:
                prev_dt = parse_ts(prev)
                for m in msgs:
                    ts = m.get("ts") or m.get("time") or m.get("timestamp")
                    if not ts:
                        continue
                    try:
                        if parse_ts(str(ts)) > prev_dt:
                            new_n += 1
                    except Exception:  # noqa: BLE001 - 时间格式异常不影响导出
                        continue
            else:
                new_n = len(msgs)
            fresh[gid] = new_n
            print(f"  → {len(msgs)} 条（其中新 {new_n}）")
            written.append(out)

    if args.mark_inactive:
        newly = {g: n for g, n in fresh.items() if n == 0}
        if newly:
            data = {}
            if INACTIVE.is_file():
                try:
                    data = json.loads(INACTIVE.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    data = {}
            data.setdefault("_doc", "不活跃群：增量导出与导入默认跳过。"
                                    "要重新纳入用 --include-inactive。")
            data.setdefault("_how_to_update", "手工维护；某群重新活跃时删掉它那一行。")
            bucket = data.setdefault(NAMESPACE, {})
            today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            for gid, _n in newly.items():
                bucket[str(gid)] = {
                    "active": False,
                    "last_message": lt.get(gid),
                    "marked_at": today,
                    "reason": f"{today} 增量导出 0 条新消息",
                }
            INACTIVE.parent.mkdir(parents=True, exist_ok=True)
            INACTIVE.write_text(
                json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8")
            print(f"\n已标记不活跃 {len(newly)} 个：{sorted(newly)}")
        else:
            print("\n本次没有 0 新消息的群，清单未改")

    # 用完就删：qq-mcp 每次读库都会留**明文**副本（实测累积 3.3 GB），
    # 原库还在、随时能重新解密，所以默认清掉。
    if not args.keep_plain:
        try:
            sys.path.insert(0, str(ROOT / "harness"))
            from clean_qq_plain import clean

            rep = clean(apply=True)
            if rep["skipped_reason"]:
                print(f"\n⚠ 未清理解密副本：{rep['skipped_reason']}")
            elif rep["files"]:
                print(f"\n已清理解密副本 {len(rep['files'])} 个，"
                      f"释放 {rep['freed_bytes'] / 1024 / 1024:.1f} MB")
            else:
                print("\n解密副本：无需清理")
        except Exception as exc:  # noqa: BLE001 - 清理失败不该让导出失败
            print(f"\n⚠ 清理解密副本失败（忽略）：{exc}")

    print(f"\n完成 {len(written)}/{len(plan)} 个群；groups={groups_arg} since_min={since_arg}")
    print(f"语料目录：{out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
