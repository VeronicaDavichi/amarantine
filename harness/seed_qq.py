#!/usr/bin/env python3
"""把 mcp-qq 导出的 QQ 语料灌进记忆库（`POST /admin/ingest`）。

配合 `harness/export_qq.mjs` 使用：后者把 QQ 本地库导成 corpus JSON，
本脚本按会话逐个提交，复用 `seed_dingtalk.py` 的清洗/名册/轮询逻辑
（`source="qq"`、`dedupe_key` 前缀 `qq:`）。

用法（仓库根目录）：

    # 先起中间件 + 管理通道，再灌
    ./harness/dev-linux.sh python ../harness/seed_qq.py --corpus-dir harness/corpus/qq
    ./harness/dev-linux.sh python ../harness/seed_qq.py --corpus-dir harness/corpus/qq --dry-run
    ./harness/dev-linux.sh python ../harness/seed_qq.py --corpus-dir harness/corpus/qq --only 485490399

为什么 `--only`：先灌一个群验证管线，比一口气推 12 个群好排查。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_dingtalk import (  # noqa: E402
    collect_roster,
    get_job,
    load_turns,
    post_ingest,
)

from memory.config import load_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把 QQ 语料导入记忆库")
    ap.add_argument("--corpus-dir", required=True, help="export_qq.mjs 的输出目录")
    ap.add_argument("--namespace", default="qq")
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default="", help="只灌这个群号（文件名里的数字）")
    ap.add_argument("--wait", type=int, default=300, help="等单个 job 的最长秒数")
    ap.add_argument("--suffix", default="", help="dedupe_key 后缀（failed 也幂等，重试要换）")
    ap.add_argument(
        "--window",
        type=int,
        default=0,
        help="按 N 条一轮切窗提交（0=整会话一次）。大会话必须切："
        "整段喂给 LLM 会超时降级成一张原文卡（实测 506 轮 → 17k 字巨卡）",
    )
    args = ap.parse_args()

    settings = load_settings()
    token = getattr(settings, "ADMIN_TOKEN", "") or ""
    if not token and not args.dry_run:
        print("✗ 没配 ADMIN_TOKEN，无法调管理接口")
        return 1

    root = Path(args.corpus_dir)
    if not root.is_dir():
        print(f"✗ 目录不存在: {root}")
        return 1

    files = sorted(root.glob("*.json"))
    if args.only:
        files = [f for f in files if args.only in f.stem]
    if not files:
        print(f"✗ {root} 里没有匹配的 corpus")
        return 1

    failures = 0
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        cid, title, turns = load_turns(path)
        roster = collect_roster(data)
        print("=" * 68)
        print(f"{path.name} | {title} | {len(turns)} 条 | 名册 {len(roster)} 个")
        if not turns:
            print("  空会话，跳过")
            continue
        if args.dry_run:
            print(f"  dry-run 首条: {turns[0]['ts']} {turns[0]['text'][:60]}")
            continue

        batches = [turns]
        if args.window and len(turns) > args.window:
            batches = [
                turns[i : i + args.window]
                for i in range(0, len(turns), args.window)
            ]
            print(f"  切窗：{len(batches)} 批 × ≤{args.window} 条")

        for bi, batch in enumerate(batches, 1):
            # 切窗后每批是独立会话：cid 加 #wN，dedupe_key 才不会互相顶掉
            bcid = cid if len(batches) == 1 else f"{cid}#w{bi}"
            resp = post_ingest(
                args.base_url,
                token,
                args.namespace,
                bcid,
                batch,
                suffix=args.suffix,
                roster=roster,
                source="qq",
                key_prefix="qq",
            )
            body = resp["body"]
            print(
                f"  [{bi}/{len(batches)}] HTTP {resp['status']} "
                f"{json.dumps(body, ensure_ascii=False)[:120]}"
            )
            if resp["status"] >= 400:
                failures += 1
                continue

            job_id = body.get("job_id")
            if not job_id:
                continue

            deadline = time.time() + args.wait
            while time.time() < deadline:
                job = get_job(args.base_url, token, job_id)
                st = job.get("status")
                if st in ("done", "succeeded", "failed", "error"):
                    print(
                        f"      {st} | 建卡 {job.get('events_created')} | "
                        f"丢 {job.get('dropped_events')} | 轮次 {job.get('split_rounds')}"
                    )
                    if st in ("failed", "error"):
                        failures += 1
                    break
                time.sleep(5)
            else:
                print(f"      ✗ 等超时（{args.wait}s）")
                failures += 1

    print("=" * 68)
    print(f"完成：{len(files)} 个会话，失败 {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
