#!/usr/bin/env python3
"""从已安装的 venv 里读依赖与许可证，生成清单（spec 049 G4）。

为什么从**安装后的元数据**读，而不是从 uv.lock 猜：
`uv.lock` 只有包名/版本，许可证在包元数据里（`METADATA: License-Expression` /
`Classifier: License ::`）。装了一遍就已经是事实。
"""

from __future__ import annotations

import json
import pathlib
import sys
from importlib.metadata import distributions


def license_of(dist) -> str:
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    lic = (meta.get("License") or "").strip()
    if lic and len(lic) < 60 and "\n" not in lic:
        return lic
    tags = [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    if tags:
        return tags[0].split("::")[-1].strip()
    if lic:
        return "见包内 LICENSE"
    return "未标注"


def main() -> int:
    rows = []
    for dist in distributions():
        name = dist.metadata.get("Name") or ""
        if not name:
            continue
        rows.append((name.lower(), dist.version, license_of(dist)))
    rows.sort()
    out = pathlib.Path("/tmp/deps.json")
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(rows)} 个包 → {out}")

    # 按许可证归类，方便看「有没有 GPL 传染」
    groups: dict[str, list[str]] = {}
    for name, ver, lic in rows:
        groups.setdefault(lic, []).append(f"{name}=={ver}")
    for lic, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"\n{lic}  ({len(names)})")
        print("   " + ", ".join(names[:8]) + (" …" if len(names) > 8 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
