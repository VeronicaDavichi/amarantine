#!/usr/bin/env python3
"""spec 055 小批验证：用新代码 ingest 一批真实原文，检查每卡的 raw_quote_ids。

## 验证什么

修复前：每卡挂整批 30 条（实测 P50=P90=max=30）
修复后：每卡只挂它 source_indices 指的几条（预期 1-5）

同时看摘要是否更具体（不再是「群友在聊天」这种空话）。

## 为什么不污染生产

用临时 namespace `verify055`，跑完可 purge。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

sys.path.insert(0, "/mnt/e/PycharmProjects/amarantine/back_end/src")
sys.stdout.reconfigure(encoding="utf-8")

NS = "verify055"
BASE = "http://127.0.0.1:8799"


def _token() -> str:
    import pathlib

    for line in pathlib.Path(
            "/mnt/e/PycharmProjects/amarantine/back_end/.env").read_text(
                encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("找不到 ADMIN_TOKEN")


TOKEN = _token()


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())


def get(path: str) -> dict:
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {TOKEN}"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())


def main() -> int:
    # 取一批真实原文（生产库，一个 session 的前 60 条）
    import sqlite3

    from memory.services.local_paths import load_settings_from_project
    from memory.services.raw_quote import db_path_from_settings

    s = load_settings_from_project()
    con = sqlite3.connect(f"file:{db_path_from_settings(s)}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT text, ts, session_id FROM raw_quote "
        "WHERE namespace='qq-10000000001' AND session_id IN "
        "(SELECT session_id FROM raw_quote WHERE namespace='qq-10000000001' "
        " GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1) "
        "ORDER BY ts LIMIT 60").fetchall()
    con.close()
    print(f"取到 {len(rows)} 条真实原文（来自最大的 session）")
    for t, ts, _sid in rows[:3]:
        print(f"  · {t[:50]}")

    turns = [{"role": "user", "text": t, "ts": ts} for t, ts, _s in rows]
    print(f"\n提交 ingest → namespace={NS}")
    try:
        job = post("/admin/ingest", {
            "namespace": NS, "dedupe_key": f"v055-{int(time.time())}",
            "source": "verify", "session_id": "s055",
            "turns": turns,
        })
    except Exception as exc:  # noqa: BLE001
        print(f"✗ ingest 失败: {type(exc).__name__}: {exc}")
        return 1
    jid = job.get("job_id")
    print(f"  job={jid} status={job.get('status')}")

    for i in range(1, 61):
        time.sleep(5)
        st = get(f"/admin/ingest/{jid}")
        status = st.get("status")
        if status in ("done", "failed"):
            print(f"  t+{i*5}s status={status} events={st.get('events_created')} "
                  f"error={(st.get('error') or '')[:80]}")
            break
    else:
        print("  ✗ 超时未完成")
        return 1

    # 检查生成的卡
    cards = get(f"/admin/events?namespace={NS}&limit=200").get("items") or []
    print(f"\n生成了 {len(cards)} 张卡")
    if not cards:
        print("✗ 没有卡")
        return 1

    lens = [len(c.get("raw_quote_ids") or []) for c in cards]
    import statistics

    print(f"\n{'指标':<22}{'值':>10}")
    print(f"{'卡数':<22}{len(cards):>10}")
    print(f"{'每卡原文条数 P50':<22}{statistics.median(lens):>10.0f}")
    print(f"{'每卡原文条数 均值':<22}{statistics.mean(lens):>10.1f}")
    print(f"{'每卡原文条数 max':<22}{max(lens):>10}")
    print(f"{'挂整批(60条)的卡':<22}{sum(1 for n in lens if n >= 60):>10}")

    print("\n前 8 张（摘要 + 挂了几条）:")
    for c in cards[:8]:
        n = len(c.get("raw_quote_ids") or [])
        print(f"  [{n:>2} 条] {(c.get('summary') or '')[:56]}")

    print(f"\n清理：python3 harness/purge_namespace.py --namespace {NS} "
          f"--confirm 'purge {NS}' --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
