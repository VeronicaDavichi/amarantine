"""找出 config.py 里「有定义、无人用」的字段 —— 假实现的通用检测。

这是 A10/A16/A27/A4 四起假实现的共同模式：
配置项有默认值，消费它的逻辑不存在。用一条命令全量扫一遍。
"""

import pathlib
import re

ROOT = pathlib.Path("/mnt/e/PycharmProjects/amarantine/back_end/src")
config_path = ROOT / "memory" / "config.py"
text = config_path.read_text()

# 顶层字段：4 空格缩进 + 全大写 + 冒号
fields = re.findall(r"^    ([A-Z][A-Z0-9_]*)\s*:", text, re.M)

all_py = [p for p in ROOT.rglob("*.py") if p != config_path]
corpus = {}
for p in all_py:
    try:
        corpus[p] = p.read_text(errors="ignore")
    except OSError:
        pass

unused = []
used_only_in_comments = []
for f in fields:
    hits = []
    for p, src in corpus.items():
        if re.search(rf"\b{re.escape(f)}\b", src):
            hits.append(p.relative_to(ROOT).as_posix())
    if not hits:
        unused.append(f)
    else:
        # 只出现在注释里也算「没人用」
        code_hits = [
            p for p in hits if re.search(rf"^\s*[^#\n]*\b{re.escape(f)}\b", corpus[pathlib.Path(ROOT / p)], re.M)
        ]
        if not code_hits:
            used_only_in_comments.append(f)

print(f"config 字段总数 : {len(fields)}")
print(f"零引用（真·假实现） : {len(unused)}")
for u in sorted(unused):
    print(f"   - {u}")
print(f"\n只在注释里出现 : {len(used_only_in_comments)}")
for u in sorted(used_only_in_comments):
    print(f"   - {u}")
