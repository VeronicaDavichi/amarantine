#!/usr/bin/env python3
"""把生产 namespace 的原文重灌进新 namespace（spec 055 方案 B）。

## 为什么要重灌

`raw_quote_ids` 之前整批挂载（每卡 30 条），修好后每卡只挂自己的 2-3 条。
但存量卡的 `source_indices` 没存下来，无法就地修复 —— 只能重新切分。

## 为什么灌新 namespace 而不是原地

旧数据完整保留，出问题能立刻切回；还能直接对比两套的召回指标。

## 做法

从 SQLite 读生产 namespace 的原文（**只读，不动旧的**），
按 session 分组、每批 BATCH 条，POST /admin/ingest 到新 namespace。

保留原始 `ts`（时间不能变）与 `session_id`（切分要上下文）。

## 续跑

进度记在 JSON 文件里（已提交的 `session:批次` 键），重跑会跳过。

用法：

    python3 harness/reingest_namespace.py --from qq-10000000001 --to qq-10000000001-v2
    python3 harness/reingest_namespace.py ... --apply          # 真灌
    python3 harness/reingest_namespace.py ... --apply --limit-sessions 1   # 小批
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "back_end" / "src"))

BASE = "http://127.0.0.1:8799"
PROGRESS = pathlib.Path("/tmp/reingest_progress.json")
BATCH = 40  # 每批 40 条（60 条实测 25 秒，40 条更稳）


def token() -> str:
    for line in (ROOT / "back_end" / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("找不到 ADMIN_TOKEN")


TOKEN = token()


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"})
    return json.loads(urllib.request.urlopen(req, timeout=600).read())


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    return json.loads(urllib.request.urlopen(req, timeout=600).read())


def load_progress() -> set[str]:
    if PROGRESS.is_file():
        try:
            return set(json.loads(PROGRESS.read_text()))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def save_progress(done: set[str]) -> None:
    PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="重灌原文到新 namespace（spec 055）")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--limit-sessions", type=int, default=0, help="只做前 N 个 session")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--max-batches", type=int, default=0,
                    help="只提交前 N 批（试跑用）")
    args = ap.parse_args()

    from memory.services.local_paths import load_settings_from_project
    from memory.services.raw_quote import db_path_from_settings

    s = load_settings_from_project()
    con = sqlite3.connect(f"file:{db_path_from_settings(s)}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT session_id, ts, text, role FROM raw_quote "
        "WHERE namespace=? ORDER BY session_id, ts", (args.src,)).fetchall()
    con.close()
    print(f"源 namespace {args.src}：{len(rows)} 条原文")

    # 按 session 分组
    by_sess: dict[str, list[tuple]] = {}
    for sid, ts, text, role in rows:
        if not (text or "").strip():
            continue
        by_sess.setdefault(str(sid or "unknown"), []).append((ts, text, role))

    sessions = sorted(by_sess.items(), key=lambda kv: -len(kv[1]))
    if args.limit_sessions:
        sessions = sessions[: args.limit_sessions]

    total_batches = sum(
        (len(v) + args.batch - 1) // args.batch for _k, v in sessions)
    print(f"{len(sessions)} 个 session，共 {total_batches} 批（批大小 {args.batch}）")
    print(f"目标 namespace：{args.dst}")
    print(f"预计：{total_batches} × 约 30-40 秒 ≈ {total_batches*35/60:.0f} 分钟\n")

    if not args.apply:
        print("（预演；加 --apply 才真灌）")
        for sid, items in sessions[:5]:
            print(f"  {sid:<22}{len(items):>5} 条 → "
                  f"{(len(items)+args.batch-1)//args.batch} 批")
        return 0

    done = load_progress() if args.resume else set()
    stats = {"submitted": 0, "skipped": 0, "failed": 0, "events": 0}
    t0 = time.monotonic()
    n_done = 0

    for sid, items in sessions:
        for bi in range(0, len(items), args.batch):
            key = f"{sid}:{bi}"
            n_done += 1
            if key in done:
                stats["skipped"] += 1
                continue
            chunk = items[bi:bi + args.batch]
            turns = [{"role": (r or "user"), "text": t, "ts": ts}
                     for ts, t, r in chunk if ts]
            if not turns:
                turns = [{"role": (r or "user"), "text": t}
                         for ts, t, r in chunk]
            try:
                job = post("/admin/ingest", {
                    "namespace": args.dst,
                    "dedupe_key": f"reing-{sid}-{bi}",
                    "source": "reingest",
                    "session_id": sid,
                    "turns": turns,
                })
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                print(f"  ! {key} 提交失败: {type(exc).__name__}: {exc}")
                continue
            stats["submitted"] += 1
            jid = job.get("job_id")
            # 等这个 job 完成再进下一批（避免并发打爆 LLM 网关）
            for _ in range(120):
                time.sleep(3)
                try:
                    st = get(f"/admin/ingest/{jid}")
                except Exception:  # noqa: BLE001
                    continue
                if st.get("status") in ("done", "failed"):
                    stats["events"] += int(st.get("events_created") or 0)
                    if st.get("status") == "failed":
                        stats["failed"] += 1
                        print(f"  ! {key} job failed: {(st.get('error') or '')[:80]}")
                    break
            done.add(key)
            save_progress(done)
            if args.max_batches and stats["submitted"] >= args.max_batches:
                print(f"  已达 --max-batches={args.max_batches}，停止")
                save_progress(done)
                print(f"  统计：提交 {stats['submitted']} 批，建 {stats['events']} 卡")
                return 0 if stats["failed"] == 0 else 1
            if n_done % 10 == 0:
                el = time.monotonic() - t0
                print(f"  ...{n_done}/{total_batches} 批  {el/60:.1f} 分钟  "
                      f"已建 {stats['events']} 卡  失败 {stats['failed']}",
                      flush=True)

    print(f"\n{'指标':<14}{'值':>10}")
    print(f"{'提交批数':<14}{stats['submitted']:>10}")
    print(f"{'跳过(续跑)':<14}{stats['skipped']:>10}")
    print(f"{'失败':<14}{stats['failed']:>10}")
    print(f"{'新建卡片':<14}{stats['events']:>10}")
    print(f"{'耗时':<14}{(time.monotonic()-t0)/60:>9.1f} 分")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
