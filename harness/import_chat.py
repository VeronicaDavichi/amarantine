#!/usr/bin/env python3
"""历史聊天导入工具（spec 040）。

一个入口把「语料 → 规范化 → 隐私闸门 → 切窗 → 提交 → 报告」串起来，
替代之前每组一份的一次性脚本。

用法：

    # 干跑：只看会切几片、拦几条，不提交
    python ../harness/import_chat.py --corpus-dir harness/corpus/dingtalk --dry-run

    # 真灌：写到指定 namespace
    python ../harness/import_chat.py --corpus-dir harness/corpus/dingtalk \\
        --namespace njzq-dingtalk --identity data/corpus/identity.json --window 30

    # 只灌某一个文件
    python ../harness/import_chat.py --corpus data/corpus/x.json --window 30

    # 续跑：跳过已经 done 的窗口（按 dedupe_key 判断）
    python ../harness/import_chat.py --corpus-dir ... --resume

报告写在 `--report`（默认 `back_end/data/import_report.json`）。

## 与旧脚本的关系

`seed_dingtalk.py` / `seed_qq.py` 是**单文件、单批次**的版本；
本工具在它们之上补了：隐私闸门、切窗、续跑、汇总报告、事件时间贯通。
旧脚本保留（0034/040 的历史记录），但日常导入走这一个入口。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.request
import sys
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "back_end" / "src"))

import httpx  # noqa: E402

from memory.services.importer import (  # noqa: E402
    chunk_turns,
    dedupe_key_for,
    normalize_message,
    privacy_hits,
)


def senders_of(chunk: list[dict]) -> set[str]:
    """从 turn 文本里取发言人（`王洁：内容` → `王洁`）。"""
    out: set[str] = set()
    for turn in chunk:
        text = str(turn.get("text") or "")
        if "：" in text:
            name = text.split("：", 1)[0].strip()
            if 2 <= len(name) <= 6:
                out.add(name)
    return out


def roster_for(chunk: list[dict], idents: list) -> list:
    """只把**本片出现的人**放进名册。

    不过滤的代价：474 个联系人全被建成 `Person` 与 `Account`（实测），
    图被灌进一堆从没出现过的人，`OPERATED_BY` 边也凭空多 474 条。
    """
    if not idents:
        return []
    senders = senders_of(chunk)
    picked = [i for i in idents if set(i.candidate_names()) & senders]
    return [
        {"uid": i.uid, "nick": i.nick, "remark": i.remark, "real_name": i.real_name,
         "platform": i.platform, "source": "identity_file"}
        for i in picked
    ]


def load_corpus(path: pathlib.Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "messages" not in data:
        raise SystemExit(f"✗ {path.name}: 缺少 messages")
    return data


def build_turns(data: dict, *, min_len: int = 4) -> tuple[list[dict], list[dict]]:
    """语料 → 合法 turn 列表 + 被过滤项（带原因）。"""
    turns: list[dict] = []
    dropped: list[dict] = []
    for m in data["messages"]:
        ts = m.get("ts")
        raw = {"content": m, "contentType": m.get("type")}
        text, reason = normalize_message(raw)
        if text is None:
            dropped.append({"ts": ts, "reason": reason})
            continue
        hits = privacy_hits(text)
        if hits:
            dropped.append({"ts": ts, "reason": ",".join(hits), "text": text[:30]})
            continue
        if len(text) < min_len:
            dropped.append({"ts": ts, "reason": "过短"})
            continue
        # QQ 语料里 sender 可能是数字 QQ 号，统一转成字符串
        sender = str(m.get("sender") or "").strip()
        turns.append({
            "text": f"{sender}：{text}" if sender else text,
            "role": "user",
            "ts": ts,
        })
    return turns, dropped


def failed_keys(db_path: pathlib.Path) -> set[str]:
    """已经 failed 的 dedupe_key（重试要换后缀，否则服务端幂等返回旧的 failed）。"""
    import sqlite3

    if not db_path.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT namespace || '|' || dedupe_key FROM ingest_job WHERE status IN ('failed','error')"
        ).fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:  # noqa: BLE001
        return set()


def load_memory_lexicon(
    namespaces: list[str],
    *,
    uri: str = "http://127.0.0.1:7474/db/neo4j/tx/commit",
    user: str = "neo4j",
    password: str = "password",
) -> str:
    """把若干命名空间的**事件摘要 + 实体/人名**拼成一个大字符串。

    为什么不用 `/admin/recall`：召回分数是 RRF 归一化后的**相对值**
    （top-1 恒为 1.0），拿它判「相关」等于恒真 —— 实测 289 条广告全部被判相关。
    词面命中没有这个问题：广告里的品牌/作品名要么在记忆里出现过，要么没有。
    """
    import base64
    import urllib.request

    cypher = (
        "MATCH (e:Event) WHERE e.namespace IN $ns RETURN e.summary AS t "
        "UNION MATCH (n) WHERE n.namespace IN $ns AND (n:Entity OR n:Person) "
        "RETURN n.name AS t"
    )
    body = json.dumps({"statements": [{"statement": cypher, "parameters": {"ns": namespaces}}]})
    req = urllib.request.Request(uri, data=body.encode())
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
        rows = data["results"][0]["data"]
        return "\n".join(str(r["row"][0] or "") for r in rows)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 读取记忆词库失败（按空处理）: {exc}")
        return ""


def gate_marketing(
    turns: list[dict],
    *,
    lexicon: str,
    dropped: list[dict],
) -> tuple[list[dict], dict]:
    """营销内容 → 词面命中已有记忆才保留。

    判据：营销消息抽出的**术语**（品牌/作品名，已去掉 URL/图片哈希/模板词）
    只要有一个出现在已有记忆里就保留；一个都没有 = 与用户关心的事无关，丢掉。

    Returns:
        `(kept_turns, stats)`。
    """
    from memory.services.marketing import looks_like_marketing, recall_terms

    stats = {"marketing": 0, "kept": 0, "dropped": 0, "cache_hits": 0}
    cache: dict[tuple[str, ...], bool] = {}
    kept: list[dict] = []

    for turn in turns:
        hit, _detail = looks_like_marketing(turn.get("text") or "")
        if not hit:
            kept.append(turn)
            continue
        stats["marketing"] += 1
        terms = tuple(recall_terms(turn.get("text") or ""))
        if not terms:
            stats["dropped"] += 1
            dropped.append({"ts": turn.get("ts"), "reason": "marketing_no_terms"})
            continue
        if terms in cache:
            stats["cache_hits"] += 1
            related = cache[terms]
        else:
            related = any(t in lexicon for t in terms)
            cache[terms] = related
        if related:
            stats["kept"] += 1
            kept.append(turn)
        else:
            stats["dropped"] += 1
            dropped.append({
                "ts": turn.get("ts"),
                "reason": "marketing_unrelated",
                "terms": list(terms),
            })
    return kept, stats


def done_keys(db_path: pathlib.Path) -> set[str]:
    """读本地 job 表，拿到已经完成的 dedupe_key（续跑用）。"""
    import sqlite3

    if not db_path.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT namespace || '|' || dedupe_key FROM ingest_job WHERE status IN ('done','succeeded')"
        ).fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:  # noqa: BLE001 - 续跑是增强，读不到就全跑
        return set()


def post_ingest(base_url: str, token: str, payload: dict) -> dict:
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{base_url}/admin/ingest",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code >= 400:
        raise SystemExit(f"✗ 提交失败 {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def wait_job(base_url: str, token: str, job_id: str, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        with httpx.Client(timeout=30.0) as client:
            job = client.get(
                f"{base_url}/admin/ingest/{job_id}",
                headers={"Authorization": f"Bearer {token}"},
            ).json()
        if job.get("status") != last:
            print(f"    status={job.get('status')}")
            last = job.get("status")
        if job.get("status") in ("done", "succeeded", "failed", "error"):
            return job
        time.sleep(3)
    return job


def run_selftest(base_url: str, token: str, namespace: str, path: pathlib.Path) -> dict:
    """灌完自动跑召回自测（A5）。

    自测文件格式：

        [{"q": "ESB 字段映射谁改的", "expect": ["ESB"]}]

    「命中」的定义：**任意一条材料的摘要里出现任意一个 expect 关键词**。
    这是粗判据 —— 它测的是「灌进去的东西查得回来」，不是语义正确性。
    """
    cases = json.loads(path.read_text(encoding="utf-8"))
    results = []
    hits = 0
    for case in cases:
        q = case["q"]
        expect = case.get("expect") or []
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{base_url}/admin/recall",
                json={"query": q, "namespace": namespace},
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code >= 400:
            results.append({"q": q, "hit": False, "error": resp.text[:120]})
            continue
        materials = resp.json().get("materials") or []
        text = " ".join(str(m.get("summary") or "") for m in materials)
        hit = any(k in text for k in expect) if expect else bool(materials)
        hits += int(hit)
        results.append({
            "q": q, "hit": hit, "materials": len(materials),
            "matched": [k for k in expect if k in text],
        })
    return {"total": len(cases), "hits": hits,
            "rate": round(hits / len(cases), 3) if cases else 0.0,
            "cases": results}


def main() -> int:
    ap = argparse.ArgumentParser(description="历史聊天导入（spec 040）")
    ap.add_argument("--corpus", default="", help="单个语料 JSON")
    ap.add_argument("--corpus-dir", default="", help="目录下所有 JSON")
    ap.add_argument("--namespace", default="njzq-dingtalk")
    ap.add_argument("--window", type=int, default=30, help="每片多少条（默认 30）")
    ap.add_argument("--identity", default="", help="身份记录 JSON（spec 043）")
    ap.add_argument("--prefix", default="dt", help="dedupe_key 前缀")
    ap.add_argument(
        "--suffix",
        default="",
        help="dedupe_key 后缀。上一片 failed 时用它重试（failed 也做幂等）",
    )
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--resume", action="store_true", help="跳过已 done 的窗口")
    ap.add_argument(
        "--gate-marketing",
        action="store_true",
        help="营销内容先查记忆：召不回来就不存（需要服务在跑）",
    )
    ap.add_argument("--gate-threshold", type=float, default=0.35,
                    help="召回分数门槛，低于它视为「与已有记忆无关」")
    ap.add_argument("--gate-namespaces", default="",
                    help="拿哪些记忆域比对（逗号分隔；默认目标域）")
    ap.add_argument(
        "--retry-failed",
        action="store_true",
        help="failed 的片换后缀重提（服务端对 failed 也做幂等，不改键不会重跑）",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait", type=int, default=300, help="每片等待秒数")
    ap.add_argument("--report", default=str(ROOT / "back_end" / "data" / "import_report.json"))
    ap.add_argument(
        "--selftest",
        default="",
        help="召回自测 JSON（灌完后自动跑，输出命中率）",
    )
    args = ap.parse_args()

    if not args.corpus and not args.corpus_dir:
        raise SystemExit("✗ 需要 --corpus 或 --corpus-dir")

    files: list[pathlib.Path] = []
    if args.corpus:
        files.append(pathlib.Path(args.corpus))
    if args.corpus_dir:
        files.extend(sorted(pathlib.Path(args.corpus_dir).glob("*.json")))
    if not files:
        raise SystemExit("✗ 没找到语料文件")

    from memory.config import load_settings

    settings = load_settings()
    token = getattr(settings, "ADMIN_TOKEN", "") or ""
    if not args.dry_run and not token:
        raise SystemExit("✗ 没配 ADMIN_TOKEN")

    args._lexicon = ""
    if args.gate_marketing:
        gate_ns = [n.strip() for n in args.gate_namespaces.split(",") if n.strip()]
        gate_ns = gate_ns or [args.namespace]
        print(f"加载记忆词库: {gate_ns}")
        args._lexicon = load_memory_lexicon(gate_ns)
        print(f"  词库 {len(args._lexicon)} 字符")

    idents: list = []
    if args.identity:
        from memory.services.identity import from_json_file

        idents = from_json_file(args.identity)
        print(f"身份记录 {len(idents)} 条（按每片发言人过滤后再提交）")

    db_path = pathlib.Path(settings.DATABASE_URL.replace("sqlite:///", "")).expanduser()
    if not db_path.is_absolute():
        db_path = ROOT / "back_end" / db_path
    already = done_keys(db_path) if args.resume else set()
    failed = failed_keys(db_path) if args.retry_failed else set()

    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "namespace": args.namespace,
        "window": args.window,
        "files": [],
    }
    totals = {"chunks": 0, "skipped": 0, "turns": 0, "events": 0, "dropped_events": 0}

    for path in files:
        data = load_corpus(path)
        cid = data.get("cid") or path.stem
        turns, dropped = build_turns(data)
        gate_stats = None
        if args.gate_marketing:
            turns, gate_stats = gate_marketing(turns, lexicon=args._lexicon, dropped=dropped)
            print(f"  营销闸门: 识别 {gate_stats['marketing']} 条 → "
                  f"保留 {gate_stats['kept']} / 丢弃 {gate_stats['dropped']} "
                  f"(缓存命中 {gate_stats['cache_hits']})")
        chunks = chunk_turns(turns, args.window)
        print(f"\n=== {path.name} | cid={cid} | turns={len(turns)} | 片={len(chunks)} | 过滤={len(dropped)}")

        entry = {"file": path.name, "cid": cid, "turns": len(turns),
                 "chunks": [], "dropped": dropped[:50], "dropped_total": len(dropped)}
        if gate_stats is not None:
            entry["marketing_gate"] = gate_stats

        for idx, chunk in enumerate(chunks, 1):
            key = dedupe_key_for(cid, chunk, idx, prefix=args.prefix)
            if args.suffix:
                key = f"{key}-{args.suffix}"
            # failed 的片：换后缀重提（`-r1`、`-r2`…），否则幂等直接返回旧 failed
            if failed:
                candidate, n = key, 1
                while f"{args.namespace}|{candidate}" in failed and n < 10:
                    candidate = f"{key}-r{n}"
                    n += 1
                key = candidate
            if f"{args.namespace}|{key}" in already:
                print(f"  [{idx}] 已 done，跳过 {key}")
                totals["skipped"] += 1
                entry["chunks"].append({"index": idx, "key": key, "status": "skipped"})
                continue
            payload = {
                "source": "chat",
                "session_id": cid,
                "dedupe_key": key,
                "turns": chunk,
                "namespace": args.namespace,
            }
            chunk_roster = roster_for(chunk, idents)
            if chunk_roster:
                payload["roster"] = chunk_roster
            if args.dry_run:
                print(f"  [{idx}] dry-run {len(chunk)} 条 | {key}")
                entry["chunks"].append({"index": idx, "key": key, "turns": len(chunk),
                                        "status": "dry_run"})
                totals["chunks"] += 1
                totals["turns"] += len(chunk)
                continue

            print(f"  [{idx}] 提交 {len(chunk)} 条 | {key}")
            resp = post_ingest(args.base_url, token, payload)
            job_id = resp.get("job_id")
            job = wait_job(args.base_url, token, job_id, args.wait) if job_id else resp
            entry["chunks"].append({
                "index": idx, "key": key, "job_id": job_id,
                "status": job.get("status"),
                "events": job.get("events_created", 0),
                "dropped_events": job.get("dropped_events", 0),
                "split_rounds": job.get("split_rounds", 1),
            })
            totals["chunks"] += 1
            totals["turns"] += len(chunk)
            totals["events"] += job.get("events_created", 0)
            totals["dropped_events"] += job.get("dropped_events", 0)

        report["files"].append(entry)

    if args.selftest:
        st = pathlib.Path(args.selftest)
        if st.exists():
            print(f"\n=== 召回自测（{st.name}）===")
            st_result = run_selftest(args.base_url, token, args.namespace, st)
            for case in st_result["cases"]:
                mark = "✓" if case.get("hit") else "✗"
                print(f"  {mark} {case['q']} → 材料 {case.get('materials', 0)} 条")
            print(f"  命中率 {st_result['hits']}/{st_result['total']} = {st_result['rate']}")
            report["selftest"] = st_result
        else:
            print(f"⚠ 自测文件不存在，跳过：{st}")

    report["totals"] = totals
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    out = pathlib.Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n汇总：片 {totals['chunks']}（跳过 {totals['skipped']}）| "
          f"turn {totals['turns']} | 卡 {totals['events']} | 丢 {totals['dropped_events']}")
    print(f"报告 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
