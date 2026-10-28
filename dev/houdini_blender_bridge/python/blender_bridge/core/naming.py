"""名前のサニタイズと一意化。

Blender 側アドオンの ``safe_name()`` と同じ規則にしてある。
片方だけ変えると 6.3 の照合が静かに壊れるので、変更するときは両方直すこと。
"""

from __future__ import annotations

import re

_INVALID = re.compile(r"[^A-Za-z0-9_]")
_REPEAT = re.compile(r"_+")


def sanitize(value: str, fallback: str = "material") -> str:
    """Houdini のノード名に使える形に落とす。"""
    out = _INVALID.sub("_", value or "")
    out = _REPEAT.sub("_", out).strip("_")
    if not out:
        out = fallback
    if out[0].isdigit():
        out = "_" + out
    return out


def unique(name: str, taken) -> str:
    """``taken`` (set) と衝突しない名前を返し、``taken`` に登録する。"""
    if name not in taken:
        taken.add(name)
        return name
    index = 1
    while f"{name}_{index}" in taken:
        index += 1
    result = f"{name}_{index}"
    taken.add(result)
    return result
