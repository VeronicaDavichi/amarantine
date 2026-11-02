"""实体代号（key）：把**身份**和**显示名**拆开。

## 为什么需要

原来是 `MERGE (n:Entity {namespace, name})` —— 名字既是身份又是标签。
这样有两个致命的毛病：

1. **改名做不到**。`助手` 改成 `豆包`，但 `豆包` 已经存在，MERGE 会把两个
   不同的实体并成一个，边全乱。改名这个动作在图结构上无法表达。

2. **同名不同实体分不开**。两个「豆包」（一个是产品，一个是人）永远撞在一起。

改成：

    key   = 代号，创建后永不变  → 身份
    name  = 原文，可随便改       → 显示

`MATCH ... WHERE n.name IN $names` 这些**查询一行都不用改** —— 用户输入的
就是名字，读出来的也是名字。代号只在 `MERGE`（创建身份）时用。
这就是「存代号，读出时原文替代」。

## 默认同一个名字是同一个实体

`key_for("豆包")` 是确定性的 —— 同名默认视为同一实体，否则每句提到
「豆包」的话都会长出一个新节点。

只有**改名撞名**时才需要新代号：调用方用 `fresh_key()` 另起一个，
`name` 仍是目标名。于是图上允许出现两个 `name="豆包"` 的节点，
各有各的 `key`。
"""

from __future__ import annotations

import hashlib
import uuid

#: 代号前缀，方便在图上肉眼看出来这不是名字
KEY_PREFIX = "e_"

#: 取多少位 sha256。12 位十六进制 = 48 bit，几十万实体也不会撞。
KEY_LEN = 12


def key_for(name: str) -> str:
    """由名字派生确定性代号。同名 → 同代号 → 同一个实体。"""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"{KEY_PREFIX}{digest[:KEY_LEN]}"


def fresh_key(name: str) -> str:
    """给「已经有一个同名的了」的情况另起一个代号。

    掺了随机数，所以同一名字每次调用都不同 —— 只在明确要**新建一个
    与现有实体同名但不同身份的节点**时才用这个。

    Args:
        name: 目标显示名。只参与哈希，不影响唯一性。
    """
    return key_for(f"{name}|{uuid.uuid4()}")


def is_key(value: str | None) -> bool:
    """看着像不像代号。用于把「用户输入的名字」和「内部代号」分开。"""
    if not value or not value.startswith(KEY_PREFIX):
        return False
    rest = value[len(KEY_PREFIX) :]
    return len(rest) == KEY_LEN and all(c in "0123456789abcdef" for c in rest)
