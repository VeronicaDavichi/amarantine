#!/usr/bin/env python3
"""图上的实体盘点（spec 037 T1 的基线脚本，可复跑）。

用法（back_end 目录）：
    /home/veronica/.venv-amarantine/bin/python ../harness/entity_stats.py

输出：各类节点数、拉丁字母实体、噪声型实体（文件后缀/hex 片段）。
037 之前：拉丁实体应为 0；037 之后：ESB/Kafka 这类出现，且噪声仍为 0。
"""

from __future__ import annotations

import sys
from pathlib import Path

from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "back_end" / ".env"


def _env(key: str, default: str) -> str:
    if ENV.is_file():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return default


uri = _env("NEO4J_URI", "bolt://127.0.0.1:7687")
user = _env("NEO4J_USER", "neo4j")
pwd = _env("NEO4J_PASSWORD", "password")

drv = GraphDatabase.driver(uri, auth=(user, pwd))
try:
    with drv.session() as s:
        for label in ("Event", "Person", "Entity", "Time", "Action"):
            n = s.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()["c"]
            print(f"{label:>7}: {n}")

        latin = [
            r["name"]
            for r in s.run(
                "MATCH (n:Entity) WHERE n.name =~ '.*[A-Za-z].*' "
                "RETURN n.name AS name ORDER BY name"
            )
        ]
        print(f"\n拉丁字母 Entity: {len(latin)}")
        for name in latin[:20]:
            print("   -", name)

        noise_query = (
            "MATCH (n:Entity) WHERE "
            "n.name =~ '(?i)^(jpg|jpeg|png|gif|mp4|mp3|amr|wav|pdf|zip|xml|json|https?|www|com|cn)$' "
            "OR n.name =~ '^[0-9A-Fa-f]*[0-9][0-9A-Fa-f]*$' "
            "RETURN n.name AS name"
        )
        noise = [r["name"] for r in s.run(noise_query)]
        print(f"\n噪声型 Entity: {len(noise)} {noise[:10]}")
        rel = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print(f"关系总数: {rel}")
finally:
    drv.close()

sys.exit(0)
