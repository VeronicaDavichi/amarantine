#!/usr/bin/env python3
"""Admin HTTP 端到端冒烟。

前置：`python -m memory.entrypoints.admin_http` 已在 8765 上跑着，
且 Weaviate / embedding / Neo4j 都 healthy。

用法（仓库根目录）：
    ./harness/dev-linux.sh python ../harness/e2e_admin.py

退出码 0 = 全绿。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
NAMESPACE = "work"


def _token() -> str:
    env_file = Path(__file__).resolve().parent.parent / "back_end" / ".env"
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("FATAL: back_end/.env 里没有 ADMIN_TOKEN")


def call(
    method: str, path: str, *, token: str | None = None, body: dict | None = None
) -> tuple[int, dict | str]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def main() -> int:
    token = _token()
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # 1) health 不需要认证
    code, body = call("GET", "/health")
    check("health 免认证", code == 200 and isinstance(body, dict) and body["status"] == "ok")

    # 2) 认证三态
    code, _ = call("GET", "/admin/review")
    check("无 token → 401", code == 401, str(code))
    code, _ = call("GET", "/admin/review", token="wrong")
    check("错 token → 403", code == 403, str(code))
    code, _ = call("GET", f"/admin/review?namespace={NAMESPACE}", token=token)
    check("正确 token → 200", code == 200, str(code))

    # 3) bucket 枚举
    code, _ = call("GET", "/admin/review?bucket=LABEL_REVIEW", token=token)
    check("非法 bucket → 400（A24 队列隔离）", code == 400, str(code))

    # 4) sweep 的 A12 闸门
    code, body = call(
        "POST", "/admin/forget/sweep", token=token,
        body={"dry_run": False, "namespace": NAMESPACE},
    )
    check("dry_run=false → 400（A12）", code == 400, str(code))

    code, body = call(
        "POST", "/admin/forget/sweep", token=token,
        body={"dry_run": True, "namespace": NAMESPACE},
    )
    ok = code == 200 and isinstance(body, dict) and body.get("dry_run") is True
    check("dry_run=true → 报告", ok)
    if ok:
        print(f"       分桶: {body['buckets']}")

    # 5) 不存在的卡 → 404
    code, _ = call(
        "POST", "/admin/forget", token=token,
        body={"event_id": "evt_does_not_exist_xyz", "namespace": NAMESPACE},
    )
    check("遗忘不存在的卡 → 404", code == 404, str(code))

    # 6) 非法 reason → 400
    code, _ = call(
        "POST", "/admin/forget", token=token,
        body={"event_id": "evt_x", "reason": "nuke", "namespace": NAMESPACE},
    )
    check("非法 reason → 400", code == 400, str(code))

    if failures:
        print(f"\nFAILED: {len(failures)} 项 —— {failures}")
        return 1
    print("\nOK: Admin HTTP 端到端全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
